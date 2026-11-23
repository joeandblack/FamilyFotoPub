# Albums and collections

Albums are named, ordered sets of existing photos and videos. Collections group albums, live
links to named people and other collections into a hierarchy up to four levels deep. Both are
catalog records only: they hold stable IDs, never copies, and no album or collection operation
reads, moves or rewrites a media file.

Code: `app/albums/__init__.py` (albums, `/api/albums`), `app/albums/collections.py`
(collections, `/api/album-collections`), `web/albums.mjs`, `web/album-collections.mjs`,
`web/album-cover.mjs`, `web/album-trash-warning.mjs`.

Contents: [Model](#model) · [Permissions and visibility](#permissions-and-visibility) ·
[Albums](#albums) · [Collections](#collections) · [Covers](#covers) · [Ordering](#ordering) ·
[Trash and deletion](#trash-and-deletion) · [Concurrency](#concurrency) · [Schema](#schema) ·
[API](#api) · [Tests](#tests)

## Model

```
Albums page
 ├─ collection (C1) ─┬─ collection (C2) ─ … ─ C4 ─┬─ albums
 │                   ├─ albums                    └─ people (live links)
 │                   └─ people (live links)
 └─ album ── album_items ── media (Library)
```

| Rule | Detail |
| --- | --- |
| A photo or video can be in many albums | Membership references the Library item; adding it again to the same album does nothing |
| An album can be in many collections | Adding it to another collection keeps its other memberships |
| A collection has at most one parent | Adding it under another parent **moves** it; a collection without a parent is top level |
| Up to four collection levels | C1 > C2 > C3 > C4. Albums and people are allowed at every level, including C4, and don't count towards depth; C4 can't contain a collection |
| No cycles | A collection can't contain itself or an ancestor |

Names are 1–200 characters (trimmed) and descriptions up to 4,000, for both albums and collections.

## Permissions and visibility

- Everyone signed in can view albums and collections. Administrators and regular accounts can
  create and change them. Guests are read-only: the server refuses every non-GET request from a
  guest (`Read-only account: changes are not allowed`), apart from sign-in and sign-out.
- There are no per-album permissions and no smart albums.
- Albums are shown per viewer. An item is listed, counted or used as a cover only if it isn't
  in Trash and the viewer may see its media (the same `media_visible` rule as the Library; see
  [Import](IMPORT.md#visibility)). Items the viewer can't see can't be added, removed or moved by
  that viewer.
- **People links are administrator-only**, matching People access: listing, adding, removing,
  reordering and opening linked people, and choosing a person's photo as a collection cover.

## Albums

**Creating and adding.**
- Albums > **Create album** makes an empty album with a name and optional description.
- In Library, Photos or Videos, **Select > Add to album**, or **Add to album** in the viewer,
  adds to an existing album or a **New album**.
- Each import offers **Create album**: a snapshot of that import's current, non-trashed,
  visible media (duplicate matches included once), newest capture first. Later imports aren't
  added.
- People's **Add all to album** sends up to 100,000 media IDs in one request; creating the album
  and adding its items is one transaction, so a failure leaves no empty album.
- New items are appended in the order given. Trashed media can't be added ("Restore items from
  Recently Deleted before adding them").

**Inside an album.** It opens the normal paged gallery (`/api/media?album={id}`, in album order)
with search, year and import filters.
- **Edit album** changes the name and description, and holds Adjust cover, Delete album and
  **Show on Albums page**.
- Select items to **Use as cover** (exactly one), **Add to album**, **Move to album** or
  **Remove from Album**. The viewer's folder-minus button also removes the current item from
  this album.
- **Move to album** adds to the destination (or a new album) and removes from the source in one
  transaction; if any selected item has left the source meanwhile, nothing moves. Add and Move
  pickers remember the last destination used in this browser (`localStorage`), and exclude the
  source album.
- Removing items only removes membership and asks no confirmation. The photos stay in Library
  and in other albums.

**Show on Albums page** is on by default. It can be turned off only for an album that belongs to
at least one collection. A hidden album stays in its collections and in every album picker. When
it leaves its last collection (or that collection is deleted) it is shown again automatically.

## Collections

- **Create** at the top level or inside a collection (**Add collection**, hidden at level four).
  **Add collection** also offers existing collections that can legally move there.
- **Add albums** (multi-select; albums already included are disabled) and **Remove albums**
  change only membership. On the Albums page you can also drag an album onto a collection card:
  the drop target is where the pointer is released, and dropping between cards reorders instead.
- **Add people** (multi-select) links named people. A person card shows the current name, face
  cover and visible photo count; opening it lists the person's photos, using the same
  eligibility rules as People (corrections, ignored and blurry faces, opted-out and trashed media
  excluded). Renames, cover changes and new matches appear automatically. Removing a link never
  changes face assignments. Links are scoped to the People catalog identity, so a reused person
  ID in another catalog can't take one over. If People is unavailable or a person was removed,
  the link stays visible as unavailable and can be removed; nothing is substituted.
- **Move collection** chooses a new parent, or Albums for top level, from options that respect
  the depth and cycle rules. A collection moved to a new parent goes after that parent's ordered
  children.
- **Remove from collection** (the small X on any card) removes only that membership; a child
  collection returns to the top level with its descendants.
- **Delete collection** asks for confirmation and deletes only the collection. Its child
  collections move to the top level (shown on Albums) and its albums are untouched; any album that
  was in no other collection is shown on Albums again.
- **Show on Albums page** for a collection is on by default and can be turned off only while it has
  a parent (a database constraint enforces this). Moving it to the top level shows it again. A
  hidden collection stays in its parent and in pickers.
- The header shows breadcrumbs to each ancestor and **Back to Albums**; an album opened from a
  collection offers **Back to collection**. Name and description edit inline. **Edit collection**
  holds cover controls, adding albums, people and child collections, moving, deleting and Select.

## Covers

**Album cover.** The chosen item (**Use as cover**) or, when none is chosen or it's hidden or
trashed, the first visible item in album order. **Adjust cover** opens a 4:3 preview matching the
album tiles: drag or use arrow keys to reposition, **Fill** crops to the frame or **Fit** shows
the whole image on a neutral background, zoom 1x–4x, Reset returns to centred Fill at 1x. Framing
(`mode`, `zoom`, `x`, `y`) is saved for the current cover only and reset when the cover changes;
saving framing for a cover that changed meanwhile is refused.

**Collection cover.** Until one is chosen, the automatic cover comes from the first album in
collection order that has a visible item: that album's chosen cover if it is visible, otherwise
its first visible item in album order. **Choose cover** lists the covers of the collection's
albums and, for administrators, the photo behind a linked person's face cover. Choosing a cover,
or adjusting the automatic one, **pins** that photo to the collection: removing the source album
or person, reordering or changing the source's own cover doesn't replace it. While the pinned
photo is trashed (including after permanent deletion, which keeps the media row as a tombstone) or
not visible to the viewer, it isn't shown and the automatic cover is used instead; the stored pin
itself stays. Covers are references, not copies.

## Ordering

| What | Saved in | How |
| --- | --- | --- |
| Items in an album | `album_items.position` | Drag to reorder (outside selection mode); applies across the whole album, including pages and items outside the current filters. New items are appended |
| Albums on the Albums page | `albums.sort_order` | Drag cards; unchanged by renames or content edits |
| Top-level collections on the Albums page | `album_collections.albums_sort_order` | Drag cards |
| Child collections in a parent | `album_collections.sort_order` | Drag cards inside the parent |
| Albums and people in a collection | `album_collection_members.position`, `album_collection_people.position` | Drag within each section; saved per collection, so the same album or person can sit elsewhere in another collection |

The Albums-page order and sibling order inside a parent are separate, so reordering in one view
never changes the other. A drop across sections changes nothing. Saving only rearranges the
affected cards in place, keeping loaded thumbnails.

## Trash and deletion

- Moving media to Trash from outside an album first checks its album memberships and warns
  ("Selected items belong to N album(s)…"). Trash hides an item from Library and every album; a
  failed membership check stops the move.
- Trashed items are hidden from album views, counts and covers but keep their membership, order
  and chosen cover, so restoring brings them back exactly.
- Completed permanent deletion removes the item's album memberships through the
  `album_permanent_delete` trigger on `media_trash`, because deletion keeps catalog tombstones.
- Deleting an album removes it from all collections; its media stays in Library.

## Concurrency

- Album changes lock the album row (`FOR UPDATE`). Adding items takes share locks on the media rows
  in ID order, the same order Trash and permanent deletion use. Moves between albums lock both
  albums in ID order, so opposite moves can't deadlock.
- Reordering albums takes a table lock with a five-second timeout and never locks media rows.
- Every structural collection change (create, move, edit, delete, reorder collections) runs under
  the `ALBUM_COLLECTION_HIERARCHY` advisory lock and revalidates depth and cycles after locking,
  so stale pages and simultaneous moves can't break the rules. These checks read collection rows
  only, never album contents.
- Removing albums from a collection, or deleting it, locks the affected albums before restoring
  their Albums-page visibility, so an album can never end up hidden and unreachable.

## Schema

| Upgrade | Adds |
| --- | --- |
| 012 | `albums`, `album_items` (primary key album+media, `position`, one optional `is_cover` per album) and the permanent-deletion trigger |
| 026 | `albums.sort_order` |
| 027 | `album_collections`, `album_collection_members` |
| 028 | `album_collections.parent_id` (`ON DELETE SET NULL`, not its own parent) |
| 029 | `albums.show_on_albums` |
| 030 | `album_collection_people` (collection, People catalog identity `pilot`, `person_id`, `position`) |
| 031 | `album_collections.sort_order` |
| 032 | `album_collections.show_on_albums`, `albums_sort_order`, and the rule that a hidden collection must have a parent |
| 033 | `albums.cover_framing` |
| 034 | `album_collections.cover_media_id`, `cover_framing` |

None of these change media or recognition data. Albums and collections are authoritative backup
data and are restored with the catalog.

## API

**Albums** (`/api/albums`): `GET` list · `POST` create (name, description, and either `media_ids`
or `import_job`) · `GET /{id}` · `PATCH /{id}` (name, description, `show_on_albums`) ·
`DELETE /{id}` · `POST /{id}/items` add · `POST /{id}/remove` · `POST /{id}/transfer` move to
another or a new album · `POST /{id}/items/{mid}/cover` · `PATCH /{id}/cover-framing` ·
`POST /{id}/items/{mid}/position` drag reorder (`target_id`, `after`) ·
`POST /{id}/items/{mid}/move` (`first`, `previous`, `next`, `last`) · `POST /{id}/position` reorder
on the Albums page · `POST /membership` which albums contain the given media.

**Collections** (`/api/album-collections`): `GET` top-level list · `POST` create · `GET /{id}`
(breadcrumbs, depth, child collections, albums, cover) · `PATCH /{id}` · `DELETE /{id}` ·
`POST /{id}/parent` move · `GET /{id}/parent-options`, `/{id}/child-options` · `POST /{id}/albums`,
`/{id}/remove` · `GET /{id}/people`, `/{id}/person-options`, `/{id}/people/{pid}/photos` and
`POST /{id}/people`, `/{id}/remove-people` (administrators) · `POST /{id}/cover`,
`PATCH /{id}/cover-framing` · `POST /order/root` and `/{id}/order` card reordering.

Authentication, the guest write block and same-origin checks apply to every route.

## Tests

`tests/test_albums.py` and `tests/test_album_collections.py` (disposable PostgreSQL schemas, with
`FAMILYFOTO_INTEGRATION=1`), JavaScript tests `tests/album-cover.test.mjs`,
`tests/album-destination.test.mjs`, `tests/album-trash-warning.test.mjs` and
`tests/people-album-picker.test.mjs`, and Chrome browser tests `tests/albums-browser.mjs`,
`tests/album-edit-browser.mjs` and `tests/album-cover-browser.mjs` (desktop and 390 px touch layouts
against isolated fixtures). None uses the live catalog.
