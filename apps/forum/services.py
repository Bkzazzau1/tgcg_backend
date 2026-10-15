"""Who may see, post, approve and hide in the forum. All changes go through here."""

import mimetypes
from pathlib import Path

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Count, Max, Min, Q
from django.db.models.functions import Lower
from django.utils import timezone

from apps.access import audit, engine
from apps.core.choices import GeographyLevel
from apps.core.permissions import Capability, has_capability
from apps.core.uploads import PHOTO_EXTENSIONS, VIDEO_EXTENSIONS, inspect_upload
from apps.geography.scope import default_to_actor_scope, scope_contains, within_scope_q

from .models import Attachment, ForumSettings, Post, Reply

C = Capability
P = Post.Status
R = Reply.Status
UNPUBLISHED = (P.PENDING, P.REJECTED, P.HIDDEN)


def approval_required() -> bool:
    return ForumSettings.current().approval_required


def moderates(user, post) -> bool:
    """Approving and hiding: the ward coordinator over the post's area (and
    the National Administrator, who holds every permission)."""
    return scope_contains(user, post, permission=C.MODERATE_DISCUSSION)


def visible_posts(user):
    """Published posts for everyone with a role, nationwide; a person's own
    posts; and, for those who approve, the unpublished posts of their area."""
    if not has_capability(user, C.VIEW_DISCUSSION_ROOM):
        return Post.objects.none()
    mine = Q(author=user) & ~Q(status=P.DELETED)
    review = Q(status__in=UNPUBLISHED) & within_scope_q(user, permission=C.MODERATE_DISCUSSION)
    return Post.objects.filter(Q(status=P.PUBLISHED) | mine | review)


def visible_replies(user, post):
    """Replies everyone sees; hidden ones only for their author and moderators."""
    replies = post.replies.exclude(status=R.DELETED)
    if moderates(user, post):
        return replies
    return replies.filter(Q(status=R.VISIBLE) | Q(author=user))


def assignment_tags(user, limit: int = 30) -> list[dict]:
    """Assignments tagged on published posts, the most recent first."""
    rows = (
        visible_posts(user)
        .filter(status=P.PUBLISHED)
        .exclude(assignment="")
        .annotate(key=Lower("assignment"))
        .values("key")
        .annotate(tag=Min("assignment"), posts=Count("pk"), latest=Max("published_at"))
        .order_by("-latest")[:limit]
    )
    return [{"tag": row["tag"], "posts": row["posts"]} for row in rows]


def _role_label(user) -> str:
    """The roles a person speaks in, as shown beside their name."""
    names = []
    for grant in engine.active_grants(user):
        speaks = {C.CREATE_DISCUSSION_THREAD, C.POST_DISCUSSION_REPLY} & grant.permissions
        if speaks and grant.role_name and grant.role_name not in names:
            names.append(grant.role_name)
    return ", ".join(names)[:200]


def _name(user) -> str:
    return (user.full_name or user.access_id)[:150]


def _check_upload(upload):
    extension = Path(upload.name).suffix.lower()
    if extension in PHOTO_EXTENSIONS:
        kind, megabytes = Attachment.Kind.PHOTO, settings.FORUM_MAX_PHOTO_MB
    elif extension in VIDEO_EXTENSIONS:
        kind, megabytes = Attachment.Kind.VIDEO, settings.FORUM_MAX_VIDEO_MB
    else:
        raise ValidationError({"files": f"{extension or 'This file'} is not a picture or video."})
    try:
        sha256 = inspect_upload(upload, max_bytes=megabytes * 1024 * 1024)
    except ValidationError as exc:
        raise ValidationError({"files": exc.messages}) from exc
    return upload, kind, sha256


@transaction.atomic
def create_post(*, actor, body="", assignment="", files=(), client_id=None, **scope) -> Post:
    """A post for the actor's area (theirs by default). Made at a polling unit
    while approval is on, it waits for the ward coordinator."""
    if client_id:
        existing = Post.objects.filter(client_id=client_id, author=actor).first()
        if existing is not None:
            return existing
    files = list(files)
    if len(files) > settings.FORUM_MAX_ATTACHMENTS:
        raise ValidationError(
            {"files": f"Add at most {settings.FORUM_MAX_ATTACHMENTS} pictures or videos."}
        )
    if sum(upload.size for upload in files) > settings.FORUM_MAX_POST_MB * 1024 * 1024:
        raise ValidationError(
            {"files": f"A post's files may total at most {settings.FORUM_MAX_POST_MB} MB."}
        )
    # Every file is checked before anything is stored.
    checked = [_check_upload(upload) for upload in files]

    default_to_actor_scope(actor, scope, C.CREATE_DISCUSSION_THREAD)
    post = Post(
        author=actor,
        author_name=_name(actor),
        author_role=_role_label(actor),
        body=body,
        assignment=assignment,
        client_id=client_id or None,
        **scope,
    )
    post.full_clean()
    if not post.body and not checked:
        raise ValidationError({"body": "Write something, or add a picture or video."})
    if not scope_contains(actor, post, permission=C.CREATE_DISCUSSION_THREAD):
        raise PermissionDenied("You can post only for your own area.")

    if post.scope_level == GeographyLevel.POLLING_UNIT and approval_required():
        post.status = P.PENDING
    else:
        post.status = P.PUBLISHED
        post.published_at = timezone.now()
    post.save()
    for upload, kind, sha256 in checked:
        Attachment.objects.create(
            post=post,
            kind=kind,
            file=upload,
            mime_type=mimetypes.guess_type(upload.name)[0] or "application/octet-stream",
            size=upload.size,
            content_hash=sha256,
        )
    return post


def _lock(post) -> Post:
    return Post.objects.select_for_update().get(pk=post.pk)


def _audit(actor, action, target, reason="", changes=None):
    audit.record(
        actor=actor,
        action=action,
        target_type="forumReply" if isinstance(target, Reply) else "forumPost",
        target_id=target.uuid,
        target_label=target.author_name,
        changes=changes,
        reason=reason,
    )


def _reason(reason: str) -> str:
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError({"reason": "Give a reason."})
    return reason


def _review(actor, post):
    if not moderates(actor, post):
        raise PermissionDenied("Only the ward coordinator approves or hides posts from this area.")


@transaction.atomic
def approve_post(*, actor, post) -> Post:
    post = _lock(post)
    _review(actor, post)
    if post.status != P.PENDING:
        raise ValidationError({"status": "This post is not waiting for approval."})
    now = timezone.now()
    post.status = P.PUBLISHED
    post.published_at = now
    post.reviewed_by = actor
    post.reviewed_at = now
    post.save(update_fields=["status", "published_at", "reviewed_by", "reviewed_at", "updated_at"])
    _audit(actor, "forum.postApproved", post)
    return post


@transaction.atomic
def reject_post(*, actor, post, reason: str) -> Post:
    post = _lock(post)
    _review(actor, post)
    if post.status != P.PENDING:
        raise ValidationError({"status": "This post is not waiting for approval."})
    post.review_reason = _reason(reason)
    post.status = P.REJECTED
    post.reviewed_by = actor
    post.reviewed_at = timezone.now()
    post.save(update_fields=["status", "review_reason", "reviewed_by", "reviewed_at", "updated_at"])
    _audit(actor, "forum.postRejected", post, reason=post.review_reason)
    return post


@transaction.atomic
def hide_post(*, actor, post, reason: str) -> Post:
    post = _lock(post)
    _review(actor, post)
    if post.status not in (P.PUBLISHED, P.PENDING):
        raise ValidationError({"status": "Only a published or waiting post can be hidden."})
    post.review_reason = _reason(reason)
    post.status = P.HIDDEN
    post.reviewed_by = actor
    post.reviewed_at = timezone.now()
    post.save(update_fields=["status", "review_reason", "reviewed_by", "reviewed_at", "updated_at"])
    _audit(actor, "forum.postHidden", post, reason=post.review_reason)
    return post


@transaction.atomic
def delete_post(*, actor, post) -> Post:
    """The author removes their post: its words and files go; the record of
    who posted when stays."""
    post = _lock(post)
    if post.author_id != actor.pk:
        raise PermissionDenied("Only its author can delete a post.")
    if post.status == P.DELETED:
        return post
    names = [attachment.file.name for attachment in post.attachments.all()]
    post.attachments.all().delete()
    storage = Attachment._meta.get_field("file").storage
    transaction.on_commit(lambda: [storage.delete(name) for name in names])
    post.status = P.DELETED
    post.body = ""
    post.assignment = ""
    post.save(update_fields=["status", "body", "assignment", "updated_at"])
    _audit(actor, "forum.postDeleted", post)
    return post


@transaction.atomic
def add_reply(*, actor, post, body: str) -> Reply:
    """Replies appear at once, on published posts."""
    if not has_capability(actor, C.POST_DISCUSSION_REPLY):
        raise PermissionDenied("Your role does not permit replies.")
    if post.status != P.PUBLISHED:
        raise ValidationError({"post": "Replies open once the post is published."})
    reply = Reply(
        post=post,
        author=actor,
        author_name=_name(actor),
        author_role=_role_label(actor),
        body=(body or "").strip(),
    )
    if not reply.body:
        raise ValidationError({"body": "Write a reply."})
    reply.full_clean()
    reply.save()
    return reply


@transaction.atomic
def hide_reply(*, actor, reply, reason: str) -> Reply:
    reply = Reply.objects.select_for_update().get(pk=reply.pk)
    _review(actor, reply.post)
    if reply.status != R.VISIBLE:
        raise ValidationError({"status": "This reply is not visible."})
    reply.hide_reason = _reason(reason)
    reply.status = R.HIDDEN
    reply.hidden_by = actor
    reply.hidden_at = timezone.now()
    reply.save(update_fields=["status", "hide_reason", "hidden_by", "hidden_at", "updated_at"])
    _audit(actor, "forum.replyHidden", reply, reason=reply.hide_reason)
    return reply


@transaction.atomic
def delete_reply(*, actor, reply) -> Reply:
    reply = Reply.objects.select_for_update().get(pk=reply.pk)
    if reply.author_id != actor.pk:
        raise PermissionDenied("Only its author can delete a reply.")
    if reply.status != R.DELETED:
        reply.status = R.DELETED
        reply.body = ""
        reply.save(update_fields=["status", "body", "updated_at"])
    return reply


@transaction.atomic
def set_approval_required(*, actor, required: bool) -> ForumSettings:
    """The one switch: whether posts from polling units wait for approval."""
    if not engine.is_national_administrator(actor):
        raise PermissionDenied("Only a National Administrator changes the forum settings.")
    ForumSettings.current()
    current = ForumSettings.objects.select_for_update().get(pk=1)
    if current.approval_required != required:
        before = current.approval_required
        current.approval_required = required
        current.updated_by = actor
        current.save()
        audit.record(
            actor=actor,
            action="forum.settingsChanged",
            target_type="forumSettings",
            target_id="forum",
            target_label="Forum settings",
            changes={"approval_required": {"from": before, "to": required}},
        )
    return current
