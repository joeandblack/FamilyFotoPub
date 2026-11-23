"""Who is asking. Set per web request; read when a pooled connection is checked out.

Administrators, workers, installers and scripts have no viewer and see all media.
A signed-in non-administrator sees shared media plus their own private media, and
a request with nobody signed in sees shared media only. The database function
familyfoto_can_see() (schema 36) applies this; queries call media_visible().
Trashed items additionally require the current viewer to be their deleting user
(schema 37); unrecorded historical/system deletions are administrator-only.
"""
from contextlib import contextmanager
from contextvars import ContextVar

NOBODY = ''
_viewer = ContextVar('familyfoto_viewer', default=None)


def current():
    return _viewer.get()


def for_user(user):
    if not user:
        return NOBODY
    return None if user['role'] == 'admin' else user['username']


def set_current(viewer):
    return _viewer.set(viewer)


def reset(token):
    _viewer.reset(token)


@contextmanager
def acting_as(viewer):
    token = _viewer.set(viewer)
    try:
        yield
    finally:
        _viewer.reset(token)


def media_visible(alias='m'):
    """SQL condition: the current viewer may see rows of media aliased `alias`."""
    return (f'familyfoto_can_see({alias}.owner_name,{alias}.visibility)'
            f' AND NOT EXISTS (SELECT 1 FROM media_trash viewer_trash'
            f' WHERE viewer_trash.media_id={alias}.id'
            " AND NOT familyfoto_can_see(viewer_trash.deleted_by,'private'))")


def apply(db):
    """Bind the viewer to a freshly checked-out connection (DISCARD ALL clears it on return)."""
    viewer = current()
    if viewer is None:
        return
    db.execute("SELECT set_config('familyfoto.scope','user',false),set_config('familyfoto.user',%s,false)", (viewer,))
    # A session-level SET is undone by a later rollback unless it was committed.
    db.commit()
