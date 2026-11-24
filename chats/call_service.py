"""
Hiddenly calls – ONE implementation used by both:
  • REST views  (StartCallView, LiveKitTokenView, UpdateCallStatusView, ...)
  • the global WebSocket (RealtimeConsumer: call_start, call_accept, ...)

The database (CallSession / CallParticipant) is the source of truth.
Every state change pushes realtime events to each user's devices
(`user_<id>` group) and still broadcasts the old `chat_<conversation_id>`
"call_event" frames so older app builds keep working.

All functions are synchronous (use database_sync_to_async from consumers).
"""
import json
from datetime import timedelta

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone
from livekit import api

from .models import (
    CallParticipant,
    CallSession,
    Conversation,
    ConversationMember,
    UserFCMToken,
)
from .realtime import notify_users, user_avatar_url, user_display_name

CALL_RING_TIMEOUT = timedelta(seconds=45)
BUSY_WINDOW = timedelta(hours=6)          # ignore "joined" rows older than this

ACTIVE = (CallSession.RINGING, CallSession.ACCEPTED)
FINISHED = {
    CallSession.ENDED, CallSession.REJECTED,
    CallSession.MISSED, CallSession.CANCELLED,
}
WAITING = (CallParticipant.RINGING, CallParticipant.INVITED)


class CallError(Exception):
    def __init__(self, message, status=400, **extra):
        super().__init__(message)
        self.message = message
        self.status = status
        self.extra = extra

    def as_dict(self):
        return {"error": self.message, **self.extra}


# ═════════════════════════════ lookups ═════════════════════════════
def get_call(call_id, lock=False):
    qs = CallSession.objects.select_related("conversation", "caller", "receiver")
    if lock:
        qs = qs.select_for_update()
    value = str(call_id or "").strip()
    if not value:
        return None
    if value.isdigit():
        return qs.filter(pk=int(value)).first()
    try:
        return qs.filter(call_uuid=value).first()
    except (ValidationError, ValueError):
        return None


def is_group_call(call):
    if call.conversation.type == Conversation.GROUP:
        return True
    # A private call becomes a group call once a third person is added.
    return CallParticipant.objects.filter(call=call).count() > 2


def participant_ids(call, statuses=None):
    qs = CallParticipant.objects.filter(call=call)
    if statuses:
        qs = qs.filter(status__in=list(statuses))
    return list(qs.values_list("user_id", flat=True))


def busy_call_for(user_id, exclude_call_id=None):
    qs = CallParticipant.objects.filter(
        user_id=user_id,
        status=CallParticipant.JOINED,
        call__status__in=ACTIVE,
        joined_at__gte=timezone.now() - BUSY_WINDOW,
    )
    if exclude_call_id:
        qs = qs.exclude(call_id=exclude_call_id)
    return qs.select_related("call").first()


def user_id_from_identity(identity):
    """LiveKit identity is "user-<id>"."""
    value = str(identity or "")
    if value.startswith("user-"):
        value = value[5:]
    return int(value) if value.isdigit() else None


# ═════════════════════════════ serialization ═════════════════════════════
def serialize_call(call):
    """Legacy shape (unchanged) – used by old clients and push payloads."""
    return {
        "call_id": call.id,
        "call_uuid": str(call.call_uuid),
        "conversation_id": call.conversation_id,
        "conversation_type": call.conversation.type,
        "is_group_call": call.conversation.type == Conversation.GROUP,
        "caller_id": call.caller_id,
        "receiver_id": call.receiver_id,
        "call_type": call.call_type,
        "is_video_call": call.call_type == CallSession.VIDEO,
        "status": call.status,
        "created_at": call.created_at.isoformat(),
        "answered_at": call.answered_at.isoformat() if call.answered_at else None,
        "ended_at": call.ended_at.isoformat() if call.ended_at else None,
    }


def public_call(call, group=None):
    """Legacy fields + the fields the new Flutter call screen reads."""
    if group is None:
        group = is_group_call(call)
    is_video = call.call_type == CallSession.VIDEO
    return {
        **serialize_call(call),
        "call_id": str(call.id),
        "callId": str(call.id),
        "conversation_id": str(call.conversation_id),
        "conversationId": str(call.conversation_id),
        "caller_id": str(call.caller_id),
        "is_group": group,
        "is_group_call": group,
        "is_video": is_video,
        "isVideoCall": is_video,
        "group_name": (call.conversation.name or "Group call") if group else "",
        "joined": [str(u) for u in participant_ids(call, [CallParticipant.JOINED])],
        "invited": [str(u) for u in participant_ids(call, WAITING)],
        "room": call.livekit_room_name or "",
        "livekit_room_name": call.livekit_room_name or "",
    }


def title_for(call, user, group, request=None):
    """What `user` should see as the call's name + picture."""
    conv = call.conversation
    if group:
        image = ""
        if getattr(conv, "image", None):
            try:
                from .realtime import absolute_url
                image = absolute_url(conv.image.url, request)
            except Exception:
                image = ""
        return (conv.name or "Group call"), image
    other = call.receiver if call.caller_id == user.id else call.caller
    if other is None:
        return "Call", ""
    return user_display_name(other), user_avatar_url(other, request)


# ═════════════════════════════ LiveKit ═════════════════════════════
def ensure_room(call):
    if not call.livekit_room_name:
        call.livekit_room_name = f"hiddenly-call-{call.call_uuid.hex}"
        call.save(update_fields=["livekit_room_name"])
    return call.livekit_room_name


def livekit_settings():
    url = getattr(settings, "LIVEKIT_URL", "")
    key = getattr(settings, "LIVEKIT_API_KEY", "")
    secret = getattr(settings, "LIVEKIT_API_SECRET", "")
    if not all([url, key, secret]):
        raise CallError("LiveKit settings are incomplete", 500)
    return url, key, secret


def livekit_token(call, user, request=None):
    url, key, secret = livekit_settings()
    name = user_display_name(user)
    avatar = user_avatar_url(user, request)
    metadata = json.dumps({
        "user_id": user.id,
        "full_name": name,
        "profile_picture": avatar,
        "avatar": avatar,
        "conversation_id": call.conversation_id,
        "call_id": call.id,
        "call_uuid": str(call.call_uuid),
    })
    try:
        token = (
            api.AccessToken(key, secret)
            .with_identity(f"user-{user.id}")
            .with_name(name)
            .with_metadata(metadata)
            .with_ttl(timedelta(hours=2))
            .with_grants(api.VideoGrants(
                room_join=True,
                room=ensure_room(call),
                can_publish=True,
                can_subscribe=True,
                can_publish_data=True,
            ))
            .to_jwt()
        )
    except (TypeError, ValueError) as exc:
        raise CallError("Unable to generate LiveKit token", 500, details=str(exc))
    return {
        "token": token,
        "url": url,
        # legacy names used by the old LiveKitTokenView response
        "participant_token": token,
        "server_url": url,
        "room_name": call.livekit_room_name,
        "participant_identity": f"user-{user.id}",
    }


# ═════════════════════════════ broadcasting ═════════════════════════════
def broadcast_legacy(conversation_id, data):
    """Old per-conversation group (kept for older app builds)."""
    layer = get_channel_layer()
    if layer is None:
        return
    async_to_sync(layer.group_send)(
        f"chat_{conversation_id}", {"type": "call_event", "data": data}
    )


def push_incoming(user_ids, call_data):
    from .firebase import send_incoming_call_push

    sent = 0
    for item in UserFCMToken.objects.filter(user_id__in=list(user_ids)).iterator():
        try:
            send_incoming_call_push(token=item.token, call_data=call_data)
            sent += 1
        except Exception:
            item.is_active = False
            item.save(update_fields=["is_active"])
    return sent


def _close_call(call, status, now):
    """Mark a call finished and settle every participant row."""
    call.status = status
    call.ended_at = now
    call.save(update_fields=["status", "ended_at"])
    CallParticipant.objects.filter(call=call, status=CallParticipant.JOINED).update(
        status=CallParticipant.LEFT, left_at=now
    )
    CallParticipant.objects.filter(call=call, status__in=WAITING).update(
        status=CallParticipant.MISSED if call.answered_at is None else CallParticipant.LEFT,
        left_at=now,
    )


def _emit_ended(call, reason, by=None):
    data = {
        "call_id": str(call.id),
        "callId": str(call.id),
        "conversation_id": str(call.conversation_id),
        "reason": reason,
        "status": call.status,
        "answered_at": call.answered_at.isoformat() if call.answered_at else None,
        "ended_at": call.ended_at.isoformat() if call.ended_at else None,
        "ended_by": str(by) if by else None,
    }
    notify_users(participant_ids(call), "call_ended", data, call.conversation_id)
    broadcast_legacy(call.conversation_id, {
        "type": "call_status_updated", **serialize_call(call), "action": reason,
        "updated_by": by,
    })


# ═════════════════════════════ ring timeout ═════════════════════════════
def expire_stale_ringing_calls(conversation=None):
    cutoff = timezone.now() - CALL_RING_TIMEOUT
    now = timezone.now()
    expired = []
    with transaction.atomic():
        qs = (
            CallSession.objects.select_for_update()
            .select_related("conversation")
            .filter(status=CallSession.RINGING, created_at__lt=cutoff)
        )
        if conversation is not None:
            qs = qs.filter(conversation=conversation)
        for call in qs:
            _close_call(call, CallSession.MISSED, now)
            expired.append(call)
    for call in expired:
        _emit_ended(call, "timeout")
    return len(expired)


def _repair_dead_call(call):
    """ACCEPTED but nobody joined any more → it's dead, end it quietly."""
    if call.status != CallSession.ACCEPTED:
        return False
    if CallParticipant.objects.filter(call=call, status=CallParticipant.JOINED).exists():
        return False
    _close_call(call, CallSession.ENDED, timezone.now())
    _emit_ended(call, "ended")
    return True


# ═════════════════════════════ START ═════════════════════════════
def start_call(user, conversation_id, is_video=False, request=None,
               join_existing=True, receiver_id=None):
    """Returns a dict with "event": "call_started" | "call_busy" (+ call fields).
    join_existing=True  → if the conversation already has a call, join it (socket).
    join_existing=False → raise 409 like the old REST endpoint did."""
    if not conversation_id:
        raise CallError("conversation_id is required")

    conversation = Conversation.objects.filter(id=conversation_id).first()
    if not conversation:
        raise CallError("Conversation not found", 404)

    if not ConversationMember.objects.filter(
        conversation=conversation, user=user, is_blocked=False
    ).exists():
        raise CallError("You are not an active member of this conversation", 403)

    active_members = list(
        ConversationMember.objects.filter(conversation=conversation, is_blocked=False)
        .select_related("user").order_by("id")
    )
    if len(active_members) < 2:
        raise CallError("At least two active members are required")

    receiver = None
    if conversation.type == Conversation.PRIVATE:
        others = [m for m in active_members if m.user_id != user.id]
        if len(others) != 1:
            raise CallError("Private conversation must contain exactly two active members")
        receiver = others[0].user
        if receiver_id is not None:
            try:
                if int(receiver_id) != receiver.id:
                    raise CallError("receiver_id is not the other member of this private conversation")
            except (TypeError, ValueError):
                raise CallError("Invalid receiver_id")
    elif conversation.type != Conversation.GROUP:
        raise CallError("Unsupported conversation type")

    expire_stale_ringing_calls(conversation=conversation)

    existing = (
        CallSession.objects.filter(conversation=conversation, status__in=ACTIVE)
        .order_by("-created_at").first()
    )
    if existing and _repair_dead_call(existing):
        existing = None
    if existing:
        if join_existing:
            # Group "Join", or both people pressed call at the same time.
            return join_call(user, existing.id, request=request, reply_event="call_started")
        raise CallError(
            "A call is already active in this conversation", 409,
            call_id=existing.id, call_uuid=str(existing.call_uuid), call_status=existing.status,
        )

    if busy_call_for(user.id):
        raise CallError("You are already in a call", 409)

    caller_name = user_display_name(user)
    caller_avatar = user_avatar_url(user, request)

    if receiver is not None and busy_call_for(receiver.id):
        notify_users([receiver.id], "call_missed", {
            "conversation_id": str(conversation.id),
            "caller_id": str(user.id),
            "caller_name": caller_name,
            "is_video": bool(is_video),
        }, conversation.id)
        return {"event": "call_busy", "conversation_id": str(conversation.id),
                "user_id": str(receiver.id)}

    now = timezone.now()
    try:
        with transaction.atomic():
            call = CallSession.objects.create(
                conversation=conversation,
                caller=user,
                receiver=receiver,
                call_type=CallSession.VIDEO if is_video else CallSession.AUDIO,
            )
            CallParticipant.objects.bulk_create([
                CallParticipant(
                    call=call,
                    user=m.user,
                    status=CallParticipant.JOINED if m.user_id == user.id else CallParticipant.RINGING,
                    joined_at=now if m.user_id == user.id else None,
                    is_camera_enabled=bool(is_video and m.user_id == user.id),
                )
                for m in active_members
            ])
            ensure_room(call)
    except (IntegrityError, ValidationError) as exc:
        raise CallError(
            "Unable to start call. Another active call may already exist in this conversation.",
            409, details=str(exc),
        )

    call = get_call(call.id)
    group = conversation.type == Conversation.GROUP
    invited_ids = [m.user_id for m in active_members if m.user_id != user.id]
    pub = public_call(call, group)

    # 1. ring every device of every invitee (socket)
    invite_title = (conversation.name or "Group call") if group else caller_name
    notify_users(invited_ids, "call_invite", {
        **pub,
        "caller_name": caller_name, "callerName": caller_name,
        "caller_avatar": caller_avatar, "callerAvatar": caller_avatar,
        "title": invite_title,
        "avatar": "" if group else caller_avatar,
        "conversation_name": conversation.name or "",
    }, conversation.id)

    # 2. FCM push for phones whose app is closed (same payload as before)
    legacy = {
        "type": "incoming_call",
        **serialize_call(call),
        "caller_name": caller_name,
        "caller_avatar": caller_avatar,
        "conversation_name": conversation.name or "",
    }
    sent = push_incoming(invited_ids, legacy)

    # 3. old per-conversation socket group
    broadcast_legacy(conversation.id, legacy)

    title, avatar = title_for(call, user, group, request)
    return {
        "event": "call_started",
        "success": True,
        **pub,
        "title": title,
        "avatar": avatar,
        **livekit_token(call, user, request),
        "invited_user_ids": invited_ids,
        "push_sent": sent,
    }


# ═════════════════════════════ JOIN / ACCEPT ═════════════════════════════
def join_call(user, call_id, request=None, exclude_channel=None, reply_event="call_joined"):
    """Accept (callee) or join (group / re-join). Returns token + call info.
    Returns {"event": "call_ended"} if the call is already over."""
    with transaction.atomic():
        call = get_call(call_id, lock=True)
        if not call:
            raise CallError("Call not found", 404)
        if call.status not in ACTIVE:
            return {"event": "call_ended", "call_id": str(call.id), "callId": str(call.id),
                    "conversation_id": str(call.conversation_id), "reason": "not_available",
                    "status": call.status}

        if ConversationMember.objects.filter(
            conversation=call.conversation, user=user, is_blocked=True
        ).exists():
            raise CallError("You are not allowed to join this call", 403)

        group = is_group_call(call)
        participant = (
            CallParticipant.objects.select_for_update()
            .filter(call=call, user=user).first()
        )
        if not participant:
            is_member = ConversationMember.objects.filter(
                conversation=call.conversation, user=user, is_blocked=False
            ).exists()
            if not (group and is_member):
                raise CallError("You are not allowed to join this call", 403)
            participant = CallParticipant.objects.create(
                call=call, user=user, status=CallParticipant.INVITED
            )

        if not group and participant.status in {CallParticipant.DECLINED, CallParticipant.MISSED}:
            raise CallError("You already declined or missed this call", 409)

        if busy_call_for(user.id, exclude_call_id=call.id):
            raise CallError("You are already in another call", 409)

        now = timezone.now()
        was_joined = participant.status == CallParticipant.JOINED
        participant.status = CallParticipant.JOINED
        participant.joined_at = participant.joined_at if was_joined and participant.joined_at else now
        participant.left_at = None
        participant.save(update_fields=["status", "joined_at", "left_at"])

        if user.id != call.caller_id and call.status == CallSession.RINGING:
            call.status = CallSession.ACCEPTED
            call.answered_at = now
            call.save(update_fields=["status", "answered_at"])
        ensure_room(call)

    name = user_display_name(user)
    avatar = user_avatar_url(user, request)
    pub = public_call(call, group)

    if not was_joined:
        others = [u for u in participant_ids(call, [CallParticipant.JOINED]) if u != user.id]
        notify_users(others, "call_accepted", {
            "call_id": str(call.id), "callId": str(call.id), "user_id": str(user.id),
            "name": name, "avatar": avatar, "status": call.status,
        }, call.conversation_id)
    # answered on my phone → stop ringing on my tablet / web
    notify_users([user.id], "call_answered_elsewhere", {
        "call_id": str(call.id), "callId": str(call.id),
    }, call.conversation_id, exclude_channel=exclude_channel)

    broadcast_legacy(call.conversation_id, {
        "type": "call_participant_joined",
        **serialize_call(call),
        "participant": {
            "user_id": user.id, "name": name, "profile_picture": avatar,
            "status": participant.status, "joined_at": participant.joined_at.isoformat(),
        },
    })

    title, t_avatar = title_for(call, user, group, request)
    return {
        "event": reply_event,
        **pub,
        "title": title,
        "avatar": t_avatar,
        **livekit_token(call, user, request),
    }


# ═════════════════════════════ DECLINE ═════════════════════════════
def decline_call(user, call_id, reason="declined", exclude_channel=None, as_missed=False):
    with transaction.atomic():
        call = get_call(call_id, lock=True)
        if not call:
            raise CallError("Call not found", 404)
        participant = (
            CallParticipant.objects.select_for_update()
            .filter(call=call, user=user).first()
        )
        if not participant:
            raise CallError("Not allowed", 403)
        if user.id == call.caller_id:
            raise CallError("Caller must cancel the call")
        if call.status not in ACTIVE:
            return {"event": "call_decline_ok", **public_call(call)}

        now = timezone.now()
        group = is_group_call(call)
        participant.status = CallParticipant.MISSED if as_missed else CallParticipant.DECLINED
        participant.left_at = now
        participant.save(update_fields=["status", "left_at"])

        end_status = None
        if not group:
            end_status = CallSession.MISSED if as_missed else CallSession.REJECTED
        else:
            still_ringing = CallParticipant.objects.filter(call=call, status__in=WAITING).exists()
            joined = CallParticipant.objects.filter(call=call, status=CallParticipant.JOINED).count()
            if call.status == CallSession.RINGING and not still_ringing and joined <= 1:
                end_status = CallSession.MISSED if as_missed else CallSession.REJECTED
        if end_status:
            _close_call(call, end_status, now)

    notify_users([user.id], "call_answered_elsewhere", {
        "call_id": str(call.id), "callId": str(call.id), "declined": True,
    }, call.conversation_id, exclude_channel=exclude_channel)

    if end_status:
        _emit_ended(call, "busy" if reason == "busy" else ("missed" if as_missed else "declined"),
                    by=user.id)
    else:
        notify_users(participant_ids(call, [CallParticipant.JOINED]), "call_declined", {
            "call_id": str(call.id), "callId": str(call.id), "user_id": str(user.id),
            "name": user_display_name(user), "reason": reason,
        }, call.conversation_id)
        broadcast_legacy(call.conversation_id, {
            "type": "call_status_updated", **serialize_call(call),
            "action": "missed" if as_missed else "reject",
            "updated_by": user.id, "participant_status": participant.status,
        })
    return {"event": "call_decline_ok", **public_call(call), "participant_status": participant.status}


# ═════════════════════════════ CANCEL (caller, before answer) ═════════════════════════════
def cancel_call(user, call_id, reason="cancelled", strict=False):
    """strict=True keeps the old REST rule: only a RINGING call can be cancelled."""
    ring_stopped = []
    ended = False
    with transaction.atomic():
        call = get_call(call_id, lock=True)
        if not call:
            raise CallError("Call not found", 404)
        if user.id != call.caller_id:
            raise CallError("Only caller can cancel call", 403)
        now = timezone.now()
        if call.status == CallSession.RINGING:
            _close_call(call, CallSession.MISSED if reason == "timeout" else CallSession.CANCELLED, now)
            ended = True
        elif strict:
            raise CallError("Only ringing call can be cancelled", 409)
        elif call.status == CallSession.ACCEPTED:
            # Group already running: stop ringing whoever never answered.
            ring_stopped = participant_ids(call, WAITING)
            CallParticipant.objects.filter(call=call, status__in=WAITING).update(
                status=CallParticipant.MISSED, left_at=now
            )

    if ended:
        _emit_ended(call, reason, by=user.id)
    elif ring_stopped:
        notify_users(ring_stopped, "call_ring_stopped", {
            "call_id": str(call.id), "callId": str(call.id), "reason": reason,
        }, call.conversation_id)
    return {"event": "call_cancel_ok", **public_call(call)}


# ═════════════════════════════ LEAVE / HANG UP ═════════════════════════════
def leave_call(user, call_id, reason="ended"):
    with transaction.atomic():
        call = get_call(call_id, lock=True)
        if not call:
            raise CallError("Call not found", 404)
        participant = (
            CallParticipant.objects.select_for_update()
            .filter(call=call, user=user).first()
        )
        if not participant:
            raise CallError("Not allowed", 403)
        if call.status not in ACTIVE:
            return {"event": "call_leave_ok", **public_call(call), "participant_status": participant.status}

        now = timezone.now()
        group = is_group_call(call)
        if participant.status == CallParticipant.JOINED or participant.status in WAITING:
            participant.status = CallParticipant.LEFT
            participant.left_at = now
            participant.save(update_fields=["status", "left_at"])

        end_status = None
        if not group:
            # One person leaving a private call ends it.
            end_status = (
                CallSession.CANCELLED
                if call.status == CallSession.RINGING and user.id == call.caller_id
                else CallSession.ENDED
            )
        elif user.id == call.caller_id and call.status == CallSession.RINGING:
            end_status = CallSession.CANCELLED          # caller gave up before anyone answered
        elif not CallParticipant.objects.filter(call=call, status=CallParticipant.JOINED).exists():
            end_status = CallSession.ENDED              # last person left
        if end_status:
            _close_call(call, end_status, now)

    if end_status:
        _emit_ended(call, "cancelled" if end_status == CallSession.CANCELLED else reason, by=user.id)
    else:
        notify_users(participant_ids(call, [CallParticipant.JOINED]), "call_participant_left", {
            "call_id": str(call.id), "callId": str(call.id), "user_id": str(user.id),
            "name": user_display_name(user),
        }, call.conversation_id)
        broadcast_legacy(call.conversation_id, {
            "type": "call_status_updated", **serialize_call(call), "action": "leave",
            "updated_by": user.id, "participant_status": participant.status,
        })
    return {"event": "call_leave_ok", **public_call(call), "participant_status": participant.status}


# ═════════════════════════════ ADD PEOPLE ═════════════════════════════
def add_to_call(user, call_id, user_ids, request=None):
    wanted = []
    for v in (user_ids or [])[:16]:
        try:
            uid = int(v)
        except (TypeError, ValueError):
            continue
        if uid != user.id:
            wanted.append(uid)
    if not wanted:
        raise CallError("user_ids is required")

    # Only people I share a (non-blocked) conversation with.
    my_convs = ConversationMember.objects.filter(user=user, is_blocked=False).values_list(
        "conversation_id", flat=True)
    allowed = set(ConversationMember.objects.filter(
        conversation_id__in=list(my_convs), is_blocked=False
    ).values_list("user_id", flat=True))

    busy, newly = [], []
    with transaction.atomic():
        call = get_call(call_id, lock=True)
        if not call or call.status not in ACTIVE:
            raise CallError("Call not found", 404)
        if not CallParticipant.objects.filter(
            call=call, user=user, status=CallParticipant.JOINED
        ).exists():
            raise CallError("You are not in this call", 403)

        for uid in wanted:
            if uid not in allowed:
                continue
            if busy_call_for(uid, exclude_call_id=call.id):
                busy.append(uid)
                continue
            p, created = CallParticipant.objects.get_or_create(
                call=call, user_id=uid, defaults={"status": CallParticipant.RINGING}
            )
            if created:
                newly.append(uid)
            elif p.status in {CallParticipant.DECLINED, CallParticipant.MISSED, CallParticipant.LEFT}:
                p.status = CallParticipant.RINGING
                p.left_at = None
                p.save(update_fields=["status", "left_at"])
                newly.append(uid)

    group = is_group_call(call)
    pub = public_call(call, group)
    name = user_display_name(user)
    avatar = user_avatar_url(user, request)

    if newly:
        title = (
            call.conversation.name or "Group call"
            if call.conversation.type == Conversation.GROUP
            else f"{name}'s call"
        )
        notify_users(newly, "call_invite", {
            **pub,
            "caller_name": name, "callerName": name,
            "caller_avatar": avatar, "callerAvatar": avatar,
            "title": title,
            "avatar": avatar,
            "added": True,
        }, call.conversation_id)
        push_incoming(newly, {
            "type": "incoming_call", **serialize_call(call),
            "caller_name": name, "caller_avatar": avatar,
            "conversation_name": call.conversation.name or "",
        })
        notify_users(participant_ids(call, [CallParticipant.JOINED]), "call_updated", pub,
                     call.conversation_id)

    return {"event": "call_add_result", "call_id": str(call.id),
            "ringing": [str(u) for u in newly], "busy": [str(u) for u in busy]}


# ═════════════════════════════ relay (video request etc.) ═════════════════════════════
def relay_in_call(user, call_id, event, extra=None):
    call = get_call(call_id)
    if not call or call.status not in ACTIVE:
        raise CallError("Call not found", 404)
    joined = participant_ids(call, [CallParticipant.JOINED])
    if user.id not in joined:
        raise CallError("Not in this call", 403)
    notify_users([u for u in joined if u != user.id], event, {
        "call_id": str(call.id), "callId": str(call.id),
        "from": str(user.id), "user_id": str(user.id),
        "name": user_display_name(user), **(extra or {}),
    }, call.conversation_id)
    return {"event": f"{event}_ok"}


def set_camera(user, call_id, enabled):
    CallParticipant.objects.filter(call_id=call_id, user=user).update(is_camera_enabled=bool(enabled))


# ═════════════════════════════ status / resume ═════════════════════════════
def call_status(user, conversation_id):
    if not ConversationMember.objects.filter(
        conversation_id=conversation_id, user=user, is_blocked=False
    ).exists():
        raise CallError("You are not a member of this conversation", 403)
    expire_stale_ringing_calls()
    call = (
        CallSession.objects.select_related("conversation", "caller", "receiver")
        .filter(conversation_id=conversation_id, status__in=ACTIVE)
        .order_by("-created_at").first()
    )
    if call and _repair_dead_call(call):
        call = None
    return {"event": "call_status", "conversation_id": str(conversation_id),
            "call": public_call(call) if call else None}


def pending_for_user(user, request=None):
    """On socket connect: calls still ringing for me (app opened from a push)
    and the call I'm currently in (app restarted mid-call)."""
    out = []
    since = timezone.now() - CALL_RING_TIMEOUT
    for p in (
        CallParticipant.objects.select_related("call__conversation", "call__caller", "call__receiver")
        .filter(user=user, call__status__in=ACTIVE)
        .filter(status__in=list(WAITING) + [CallParticipant.JOINED])
    ):
        call = p.call
        group = is_group_call(call)
        if p.status in WAITING and call.created_at >= since:
            caller_name = user_display_name(call.caller)
            caller_avatar = user_avatar_url(call.caller, request)
            title, _ = title_for(call, user, group, request)
            out.append(("call_invite", {
                **public_call(call, group),
                "caller_name": caller_name, "caller_avatar": caller_avatar,
                "title": title, "avatar": "" if group else caller_avatar,
            }))
        elif p.status == CallParticipant.JOINED:
            out.append(("call_resume", public_call(call, group)))
    return out


# ═════════════════════════════ LiveKit webhook ═════════════════════════════
def handle_livekit_webhook(body, auth_header):
    """Keeps the DB honest when an app crashes or loses network:
    participant_left → that user left; room_finished → call ended."""
    _url, key, secret = livekit_settings()
    receiver = api.WebhookReceiver(api.TokenVerifier(key, secret))
    event = receiver.receive(body, auth_header)   # raises if signature is wrong

    room_name = event.room.name if event.room else ""
    if not room_name:
        return
    call = CallSession.objects.filter(livekit_room_name=room_name).first()
    if not call or call.status not in ACTIVE:
        return

    if event.event == "participant_left" and event.participant:
        uid = user_id_from_identity(event.participant.identity)
        if uid is None:
            return
        joined = CallParticipant.objects.filter(
            call=call, user_id=uid, status=CallParticipant.JOINED
        ).select_related("user").first()
        if joined:
            leave_call(joined.user, call.id, reason="ended")

    elif event.event == "room_finished":
        with transaction.atomic():
            call = get_call(call.id, lock=True)
            if call.status not in ACTIVE:
                return
            # Room closed while still ringing → nobody ever connected.
            if call.status == CallSession.RINGING and call.created_at > timezone.now() - CALL_RING_TIMEOUT:
                return
            _close_call(call, CallSession.ENDED, timezone.now())
        _emit_ended(call, "ended")