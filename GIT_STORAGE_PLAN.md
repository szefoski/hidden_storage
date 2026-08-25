# Store a git repository on the hidden SD storage (bundle wrapper)

## Context

The user wants to use `git` to store a repository on the hidden storage this
project already provides (raw sector read/write over J-Link RTT, no OS-visible
device). Git can't natively treat an arbitrary block device as a remote --
it needs either a live git server speaking the pack protocol, or a
transport it already understands (like a local bundle file). Since our
"storage" is just raw sectors accessed through `sdrtt.py`, the natural fit is
**`git bundle`**: git's own format for a self-contained snapshot of refs +
objects, designed exactly for offline/removable-media transport (this is the
standard way to move a repo via a USB stick with no network). We wrap
`git bundle create`/`git fetch <bundle>` around the read/write commands
`host/sdrtt.py` already has, validated end-to-end on real hardware.

Decisions made with the user (via AskUserQuestion):
- **Approach**: a bundle wrapper script, not a full `git-remote-helper`
  protocol implementation. A custom remote helper (`git remote add card
  hidden::2000`) would give nicer syntax but our storage is a dumb blob store
  with no live git process on the other end -- push would still have to be a
  whole-bundle replace either way, so the added protocol-implementation
  complexity wouldn't buy real incremental push/pull, only UX polish. Not
  worth it for v1.
- **Timing**: queued for later, like `ENCRYPTION_PLAN.md`. This is host-only
  (no firmware changes) and *could* be built now since it sits entirely on
  top of the already-validated `write`/`read` commands -- but the user chose
  to keep all new work gated on the SD card actually arriving and being
  confirmed working, consistent with the project's established discipline of
  not layering new things on top of an as-yet-unexercised SD path.

## Design

New host-side script `host/git_hidden.py`, no firmware or protocol changes.

- `git_hidden.py push <sector> [--all | --branch NAME] [--repo PATH]`
  1. Run `git bundle create <tmpfile> <refspec>` in the target repo (default
     refspec: current branch; `--all` for everything).
  2. `sdrtt.py write <sector> -i <tmpfile>` -- reuses the existing
     length-header-based write, so no new on-disk format is needed.
  3. This is a **full-state replace**: each push overwrites whatever bundle
     was previously at that sector. Not an incremental/append transport --
     matches what a blob store can actually offer, and matches how `git
     bundle` is normally used for offline transport anyway.

- `git_hidden.py fetch <sector> [--repo PATH]`
  1. `sdrtt.py read <sector> -o <tmpfile>` (auto-sized via the existing
     length header -- no count needed).
  2. `git fetch <tmpfile>` inside the target repo -- git natively understands
     a bundle file as a fetch source, no custom protocol needed here at all.

- `git_hidden.py clone <sector> <dest-dir>`
  1. Same read-to-tempfile step as `fetch`.
  2. `git clone <tmpfile> <dest-dir>` -- git natively clones from a bundle
     file directly.

- Sector bookkeeping: same model as the rest of this project -- the caller
  picks a sector and remembers it (see `README.md`'s existing raw-storage
  usage notes). No new indexing/directory system in this pass.

- Error handling: if `sdrtt.py read` reports a clean protocol error (e.g. no
  valid header at that sector -- nothing written there yet, or garbage), or
  if `git bundle create`/`git fetch`/`git clone` themselves fail, surface
  the underlying error directly rather than swallowing it. No special
  retry/recovery logic needed beyond what `sdrtt.py` already has.

## Implementation (deferred -- do this once the SD card path is confirmed)

1. Write `host/git_hidden.py` (argparse CLI, subcommands `push`/`fetch`/
   `clone`, thin wrapper shelling out to `git` and importing `sdrtt.py`'s
   `connect()`/`transact()` or just invoking it as a subprocess -- decide
   during implementation which is cleaner; importing avoids paying the
   J-Link connect handshake twice for read-then-nothing-else calls).
2. Add a couple of hardware-free tests where possible (e.g. that the
   generated `git bundle` command line is correct, using a throwaway local
   git repo and a stub/fake for the RTT transaction layer) -- full
   push/fetch/clone round-trip needs the real SD card, same as the rest of
   `sdrtt.py`'s read/write paths.
3. Document usage in `README.md` (a short "storing a git repo" section) and
   add this to `STATUS.md`'s next-steps list, same pattern as
   `ENCRYPTION_PLAN.md`.

## Verification (once implemented, with real hardware)

1. Create a throwaway test repo with a couple of commits.
2. `git_hidden.py push <sector> --all`, then read the raw sectors back with
   `raw-read`/`dump` and confirm they hold a valid git bundle (`git bundle
   verify <file>`).
3. `git_hidden.py clone <sector> /tmp/clone-test` into a fresh directory,
   confirm `git log`/`git diff` show the same history as the source repo.
4. Make a new commit in the source repo, `push` again to the same sector,
   `fetch` into the clone, confirm the new commit shows up.
