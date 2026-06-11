from django.urls import path

from .views import (
    MyConversationsView,
    StartPrivateChatView,
    CreateGroupChatView,
    AddGroupMemberView,
    RemoveGroupMemberView,
    ConversationMessagesView,
    SendMessageView,
    EditMessageView,
    DeleteMessageView,
    MarkMessageReadView,
    MarkConversationReadView,
    SearchUserByPhoneView,
    UpdateGroupInfoView,
    BlockGroupMemberView,
    UnblockGroupMemberView,
    BlockPrivateUserView,
    UnblockPrivateUserView,
    UpdateMemberNicknameView,SaveFCMTokenView,StartCallView,UpdateCallStatusView
)

urlpatterns = [
    path("conversations/", MyConversationsView.as_view()),
    path("private/start/", StartPrivateChatView.as_view()),
    path("groups/create/", CreateGroupChatView.as_view()),
    path(
        "search-user/",
        SearchUserByPhoneView.as_view(),
        name="search-user"
    ),
    path("groups/<int:conversation_id>/add-member/", AddGroupMemberView.as_view()),
    path("groups/<int:conversation_id>/remove-member/<int:user_id>/", RemoveGroupMemberView.as_view()),
    path("groups/<int:conversation_id>/update/",UpdateGroupInfoView.as_view()),

    path("conversations/<int:conversation_id>/messages/", ConversationMessagesView.as_view()),
    path("conversations/<int:conversation_id>/send/", SendMessageView.as_view()),
    path("messages/<int:message_id>/edit/", EditMessageView.as_view()),
    path("messages/<int:message_id>/delete/", DeleteMessageView.as_view()),
    path("messages/<int:message_id>/read/", MarkMessageReadView.as_view()),
    path("conversations/<int:conversation_id>/read/", MarkConversationReadView.as_view()),
    path("conversations/<int:conversation_id>/members/<int:user_id>/block/",BlockGroupMemberView.as_view()),
    path("conversations/<int:conversation_id>/members/<int:user_id>/unblock/",UnblockGroupMemberView.as_view()),
    path("conversations/<int:conversation_id>/private/<int:user_id>/block/",BlockPrivateUserView.as_view()),
    path("conversations/<int:conversation_id>/private/<int:user_id>/unblock/",UnblockPrivateUserView.as_view()),
    path("conversations/<int:conversation_id>/members/<int:user_id>/nickname/",UpdateMemberNicknameView.as_view()),
    path("fcm-token/",SaveFCMTokenView.as_view(),name="save-fcm-token"),
    path("calls/start/", StartCallView.as_view(), name="start-call"),
    path("calls/<int:call_id>/status/", UpdateCallStatusView.as_view(), name="update-call-status"),
]