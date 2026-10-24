"""Who may see, post, comment, like, report, hide and restore in the Social
Forum. All changes go through here."""

import mimetypes
from pathlib import Path

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count, Max, Min, Q
from django.db.models.functions import Lower
from django.utils import timezone

from apps.access import audit, engine
from apps.core.choices import MembershipState
from apps.core.uploads import PHOTO_EXTENSIONS, VIDEO_EXTENSIONS, inspect_upload
from apps.geography.scope import SCOPE_FIELDS

from .models import Attachment, Like, Post, Reply, Report

P = Post.Status
R = Reply.Status
# Grants that make someone an editor: a role on their account, or an
# appointment (a position). Delegated authority alone does not.
POSITIONS = (engine.LEGACY, engine.ASSIGNMENT)


def _member(user):
    try:
        return user.member
    except Exception:  # noqa: BLE001 - no linked member
        return None


def takes_part(user) -> bool:
    """Every member with an active membership, and every account holding a
    role. A suspended or deactivated membership keeps a person out."""
    if not getattr(user, "is_authenticated", False) or not user.is_active:
        return False
    member = _member(user)
    if member is not None:
        return member.membership_state == MembershipState.ACTIVE
    return bool(engine.active_grants(user))


def is_editor(user) -> bool:
    """Everyone holding a role or position: they hide and restore posts and
    comments anywhere in Nigeria (agreed with the user on 24 October 2026)."""
    return any(grant.source in POSITIONS for grant in engine.active_grants(user))


def moderates(user, post=None) -> bool:
    """Kept for callers of the first forum: an editor moderates every post."""
    return is_editor(user)


def _require_part(user):
    if not takes_part(user):
        raise PermissionDenied("Only members with an active membership take part in the forum.")


def _require_editor(user):
    if not is_editor(user):
        raise PermissionDenied("Only someone holding a role or position can do this.")


def visible_posts(user):
    """Published posts for everyone taking part, nationwide; a person's own
    posts; and, for editors, the hidden ones."""
    if not takes_part(user):
        return Post.objects.none()
    shown = Q(status=P.PUBLISHED) | (Q(author=user) & ~Q(status=P.DELETED))
    if is_editor(user):
        shown |= Q(status=P.HIDDEN)
    return Post.objects.filter(shown)


def visible_replies(user, post):
    """Comments everyone sees; hidden ones only for their author and editors."""
    replies = post.replies.exclude(status=R.DELETED)
    if is_editor(user):
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
    """The roles and positions a person holds, as shown beside their name."""
    names = []
    for grant in engine.active_grants(user):
        if grant.source in POSITIONS and grant.role_name and grant.role_name not in names:
            names.append(grant.role_name)
    return ", ".join(names)[:200]


def _name(user) -> str:
    return (user.full_name or user.access_id)[:150]


def _where(user) -> dict:
    """The author's own area: where a member is registered, or an account's scope."""
    owner = _member(user) or user
    return {"scope_level": owner.scope_level, **{f: getattr(owner, f) for f in SCOPE_FIELDS}}


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
def create_post(*, actor, body="", assignment="", files=(), client_id=None, **_ignored) -> Post:
    """A post, published at once, from the author's own area. (An area sent
    by an older app is ignored.)"""
    _require_part(actor)
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

    now = timezone.now()
    post = Post(
        author=actor,
        author_name=_name(actor),
        author_role=_role_label(actor),
        body=body,
        assignment=assignment,
        client_id=client_id or None,
        status=P.PUBLISHED,
        published_at=now,
        **_where(actor),
    )
    post.full_clean()
    if not post.body and not checked:
        raise ValidationError({"body": "Write something, or add a picture or video."})
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


def _lock_reply(reply) -> Reply:
    return Reply.objects.select_for_update(of=("self",)).select_related("post").get(pk=reply.pk)


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


def _close_reports(actor, *, post=None, reply=None, reason=""):
    """An editor hid what was reported: its open reports close as hidden."""
    reports = Report.objects.filter(status=Report.Status.OPEN)
    reports = (
        reports.filter(reply=reply)
        if reply is not None
        else reports.filter(post=post, reply__isnull=True)
    )
    reports.update(
        status=Report.Status.CLOSED,
        outcome=Report.Outcome.HIDDEN,
        closed_by=actor,
        closed_at=timezone.now(),
        close_reason=reason,
        updated_at=timezone.now(),
    )


@transaction.atomic
def edit_post(*, actor, post, body=None, assignment=None) -> Post:
    """Its author changes the words or the assignment tag. Pictures and
    videos stay; a hidden post stays hidden."""
    post = _lock(post)
    if post.author_id != actor.pk:
        raise PermissionDenied("Only its author can edit a post.")
    if post.status == P.DELETED:
        raise ValidationError({"status": "This post was deleted."})
    before = {"body": post.body, "assignment": post.assignment}
    if body is not None:
        post.body = body
    if assignment is not None:
        post.assignment = assignment
    post.full_clean()
    if not post.body and not post.attachments.exists():
        raise ValidationError({"body": "Write something, or keep a picture or video."})
    after = {"body": post.body, "assignment": post.assignment}
    if after != before:
        post.edited_at = timezone.now()
        post.save(update_fields=["body", "assignment", "edited_at", "updated_at"])
        changes = {
            key: {"from": before[key], "to": after[key]}
            for key in after
            if before[key] != after[key]
        }
        _audit(actor, "forum.postEdited", post, changes=changes)
    return post


@transaction.atomic
def hide_post(*, actor, post, reason: str) -> Post:
    """An editor hides a post, anywhere: its author still sees it, marked
    hidden, with the reason."""
    _require_editor(actor)
    post = _lock(post)
    if post.status not in (P.PUBLISHED, P.PENDING):
        raise ValidationError({"status": "Only a published post can be hidden."})
    post.review_reason = _reason(reason)
    post.status = P.HIDDEN
    post.reviewed_by = actor
    post.reviewed_at = timezone.now()
    post.save(update_fields=["status", "review_reason", "reviewed_by", "reviewed_at", "updated_at"])
    _close_reports(actor, post=post, reason=post.review_reason)
    _audit(actor, "forum.postHidden", post, reason=post.review_reason)
    return post


@transaction.atomic
def restore_post(*, actor, post) -> Post:
    """An editor brings a hidden post back."""
    _require_editor(actor)
    post = _lock(post)
    if post.status != P.HIDDEN:
        raise ValidationError({"status": "Only a hidden post can be restored."})
    post.status = P.PUBLISHED
    post.published_at = post.published_at or timezone.now()
    post.review_reason = ""
    post.reviewed_by = actor
    post.reviewed_at = timezone.now()
    post.save(
        update_fields=[
            "status",
            "published_at",
            "review_reason",
            "reviewed_by",
            "reviewed_at",
            "updated_at",
        ]
    )
    _audit(actor, "forum.postRestored", post)
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
def add_reply(*, actor, post, body: str, parent=None) -> Reply:
    """A comment on a published post, or an answer to one of its comments;
    it appears at once. An answer to an answer joins the same comment."""
    _require_part(actor)
    if post.status != P.PUBLISHED:
        raise ValidationError({"post": "Comments are open on published posts only."})
    if parent is not None:
        if parent.post_id != post.pk or parent.status == R.DELETED:
            raise ValidationError({"parent": "Answer a comment on this post."})
        parent = parent.parent or parent
    reply = Reply(
        post=post,
        parent=parent,
        author=actor,
        author_name=_name(actor),
        author_role=_role_label(actor),
        body=(body or "").strip(),
    )
    if not reply.body:
        raise ValidationError({"body": "Write a comment."})
    reply.full_clean()
    reply.save()
    return reply


@transaction.atomic
def edit_reply(*, actor, reply, body: str) -> Reply:
    reply = _lock_reply(reply)
    if reply.author_id != actor.pk:
        raise PermissionDenied("Only its author can edit a comment.")
    if reply.status == R.DELETED:
        raise ValidationError({"status": "This comment was deleted."})
    body = (body or "").strip()
    if not body:
        raise ValidationError({"body": "Write a comment."})
    if body != reply.body:
        before = reply.body
        reply.body = body
        reply.edited_at = timezone.now()
        reply.full_clean()
        reply.save(update_fields=["body", "edited_at", "updated_at"])
        _audit(actor, "forum.replyEdited", reply, changes={"body": {"from": before, "to": body}})
    return reply


@transaction.atomic
def hide_reply(*, actor, reply, reason: str) -> Reply:
    _require_editor(actor)
    reply = _lock_reply(reply)
    if reply.status != R.VISIBLE:
        raise ValidationError({"status": "This comment is not visible."})
    reply.hide_reason = _reason(reason)
    reply.status = R.HIDDEN
    reply.hidden_by = actor
    reply.hidden_at = timezone.now()
    reply.save(update_fields=["status", "hide_reason", "hidden_by", "hidden_at", "updated_at"])
    _close_reports(actor, reply=reply, reason=reply.hide_reason)
    _audit(actor, "forum.replyHidden", reply, reason=reply.hide_reason)
    return reply


@transaction.atomic
def restore_reply(*, actor, reply) -> Reply:
    _require_editor(actor)
    reply = _lock_reply(reply)
    if reply.status != R.HIDDEN:
        raise ValidationError({"status": "Only a hidden comment can be restored."})
    reply.status = R.VISIBLE
    reply.hide_reason = ""
    reply.hidden_by = None
    reply.hidden_at = None
    reply.save(update_fields=["status", "hide_reason", "hidden_by", "hidden_at", "updated_at"])
    _audit(actor, "forum.replyRestored", reply)
    return reply


@transaction.atomic
def delete_reply(*, actor, reply) -> Reply:
    reply = _lock_reply(reply)
    if reply.author_id != actor.pk:
        raise PermissionDenied("Only its author can delete a comment.")
    if reply.status != R.DELETED:
        reply.status = R.DELETED
        reply.body = ""
        reply.save(update_fields=["status", "body", "updated_at"])
    return reply


def _likable(post=None, reply=None):
    if reply is not None:
        if reply.status != R.VISIBLE or reply.post.status != P.PUBLISHED:
            raise ValidationError({"status": "Only a visible comment can be liked."})
    elif post.status != P.PUBLISHED:
        raise ValidationError({"status": "Only a published post can be liked."})


def like(*, actor, post=None, reply=None) -> int:
    """The actor likes a post or a comment (once); returns its likes."""
    _require_part(actor)
    _likable(post, reply)
    target = {"reply": reply} if reply is not None else {"post": post}
    try:
        with transaction.atomic():
            Like.objects.get_or_create(user=actor, **target)
    except IntegrityError:  # a second tap at the same moment
        pass
    return Like.objects.filter(**target).count()


def unlike(*, actor, post=None, reply=None) -> int:
    """The actor takes back their like; returns the likes left."""
    _require_part(actor)
    target = {"reply": reply} if reply is not None else {"post": post}
    Like.objects.filter(user=actor, **target).delete()
    return Like.objects.filter(**target).count()


@transaction.atomic
def report(*, actor, post, reply=None, reason: str) -> Report:
    """Someone reports a post or comment to the editors; a second report by
    the same person while the first is open is the same report."""
    _require_part(actor)
    target = reply or post
    if target.author_id == actor.pk:
        raise ValidationError({"reason": "You cannot report your own post or comment."})
    if reply is not None and reply.status != R.VISIBLE:
        raise ValidationError({"status": "Only a visible comment can be reported."})
    if reply is None and post.status != P.PUBLISHED:
        raise ValidationError({"status": "Only a published post can be reported."})
    reason = _reason(reason)
    existing = Report.objects.filter(
        reporter=actor, post=post, reply=reply, status=Report.Status.OPEN
    ).first()
    if existing is not None:
        return existing
    try:
        with transaction.atomic():
            made = Report.objects.create(reporter=actor, post=post, reply=reply, reason=reason)
    except IntegrityError:  # the same report sent twice at the same moment
        return Report.objects.get(reporter=actor, post=post, reply=reply, status=Report.Status.OPEN)
    _audit(actor, "forum.reported", target, reason=reason)
    return made


def open_reports():
    """Reports waiting for an editor, newest first."""
    return Report.objects.filter(status=Report.Status.OPEN).select_related(
        "post", "reply", "reporter"
    )


@transaction.atomic
def dismiss_report(*, actor, report: Report, reason: str = "") -> Report:
    """An editor finds nothing wrong: the report closes, the post stays."""
    _require_editor(actor)
    report = Report.objects.select_for_update().get(pk=report.pk)
    if report.status != Report.Status.OPEN:
        raise ValidationError({"status": "This report is already closed."})
    report.status = Report.Status.CLOSED
    report.outcome = Report.Outcome.DISMISSED
    report.closed_by = actor
    report.closed_at = timezone.now()
    report.close_reason = (reason or "").strip()
    report.save(
        update_fields=["status", "outcome", "closed_by", "closed_at", "close_reason", "updated_at"]
    )
    audit.record(
        actor=actor,
        action="forum.reportDismissed",
        target_type="forumReport",
        target_id=report.uuid,
        target_label=(report.reply or report.post).author_name,
        reason=report.close_reason,
    )
    return report
