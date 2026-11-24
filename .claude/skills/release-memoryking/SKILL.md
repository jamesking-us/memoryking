---
name: release-memoryking
description: Release a new MemoryKing version. Use when the owner asks to cut, tag, publish or finish a release (PyPI package, container image, cosign signature, GitHub release). Covers what any session can prepare and what only a session on the owner's machine can publish.
---

# Releasing MemoryKing

The authority for the publishing commands is the section "Publishing a release" in `docs/deployment.md`. This skill adds the order of work, the checks before and after, and who can do which part. If the two disagree, fix the one that is wrong in the same change.

## Rules

- Publish only when the owner has asked for this release in their own words. A merged change, a finished roadmap or a passing job is not a request.
- Credentials never enter the repository, a commit message, the changelog or the chat. The PyPI token, the registry login and the signing identity stay on the owner's machine.
- Nothing is published by a hosted workflow. Every step runs locally.
- A version that has been uploaded to PyPI cannot be replaced. If anything is wrong after the upload, fix it in a new version.
- Report each step as done, failed or not attempted. Do not call a release finished while a step is open.

## Who can do what

| Part | Needs | A cloud session | A session on the owner's machine |
| --- | --- | --- | --- |
| A. Prepare, test, merge, build | the checkout, PostgreSQL 16 binaries | yes | yes |
| Tag | permission to push tags | try it; hand over if refused | yes |
| B. Publish | PyPI token, registry login, Docker, a browser for the cosign sign-in, a way to create a GitHub release | no | yes |
| C. Verify | network access to PyPI and the registry | partly | yes |

A cloud session does part A, then gives the owner the exact state (commit, tag, hashes) and stops. The owner starts a session on their own machine for part B. When two sessions work on one release, the branch `coord/windows-port` (`TO_WINDOWS.md`, `FROM_WINDOWS.md`) is the board they write to.

## Part A: prepare

1. **Decide the version** with the owner. A pre-release such as `0.3.0a1` installs only with `pip install --pre`.
2. **Check what is going out.** Every roadmap item that is in the release is `[x]`. The changelog's Unreleased section says what a user would notice. If the Windows side changed, a Windows session has run mypy, `tools/windows_tests.py` and one hook replay on the code being released.
3. **Make the release commit** on a working branch:
   - `pyproject.toml` and `src/memoryking/__init__.py`: the version.
   - `CHANGELOG.md`: leave an empty `## Unreleased`, and put what was under it beneath `## X.Y.Z (YYYY-MM-DD)` with a short opening paragraph (what the release is, the install command, the image name). That section becomes the release page text, so its links are absolute.
   - `docs/getting-started.md`: the version it names (two places).
   - `docs/roadmap.md`: done items and the phase table name the release instead of "unreleased".
   - `git grep -n "<old version>"` outside `benchmarks/`, `CHANGELOG.md` and the lock files, and read every hit.
4. **Test at an immutable commit.** Run `tools/check.py` with the project Python, then the clean-checkout job on the full commit hash:
   ```sh
   python tools/local_ci.py --revision <full sha> --output <new empty directory> --postgres-bin <PostgreSQL 16 bin directory>
   ```
   It must end with `"passed": true` in `job.json`. It builds the wheel and runs the release build on a clean clone. PostgreSQL will not run as root; run the job as an unprivileged user if the session is root.
5. **Merge to `main`** with the tested commit as the head (a squash merge with the expected head hash, if direct pushes to `main` are refused).
6. **Tag the commit on `main`** and push the tag: `git tag -a vX.Y.Z -m "MemoryKing X.Y.Z" <commit on main>`, `git push origin vX.Y.Z`. If the push is refused, say so and leave the tag to the owner's machine.
7. **Build at the tag:** `python tools/build_release.py`. It writes `dist/SHA256SUMS` and `dist/RELEASE.json`. The build is reproducible: a second run, or a run on another machine at the same commit, must give the same hashes. Give the owner the two hashes.

## Part B: publish (owner's machine)

Check out the tag, create the contributor environment, and run `python tools/build_release.py`. Compare `dist/SHA256SUMS` with the hashes from part A. If they differ, stop: the two machines did not build the same commit.

1. **PyPI.** From a throwaway environment that has `twine`: `twine upload dist/memoryking-X.Y.Z*`. Then compare the hashes at `https://pypi.org/pypi/memoryking/X.Y.Z/json` with `SHA256SUMS`.
2. **Image.** `python tools/image_rehearsal.py --output tmp/image-X.Y.Z.json` builds the local-daemon image and proves it starts. Tag and push it (`docker tag memoryking:local ghcr.io/jamesking-us/memoryking:X.Y.Z`, `docker push ...`) and note the index digest that `docker push` prints.
3. **Signature.** `cosign sign ghcr.io/jamesking-us/memoryking@sha256:<digest>`. Keyless signing opens a browser or gives a device code; the owner signs in. Then run the `cosign verify` command from `docs/deployment.md` against the digest and see it pass.
4. **GitHub release** for the tag. Notes: the version's changelog section, then the install command, the image digest and the `cosign verify` line. Attach `SHA256SUMS`, `RELEASE.json`, the wheel and the sdist. Mark it as a pre-release only when the version is one.

## Part C: verify, then close

1. In a fresh virtual environment: `pip install memoryking==X.Y.Z`, `memoryking --version`, and `memoryking setup claude-code --project scratch --dry-run` with a scratch `HOME`.
2. `docker pull` the image by digest and run `cosign verify` once more from a machine that did not sign it, if one is at hand.
3. Open the release page and the PyPI page and read them: the notes render, the links work, the four files are attached.
4. Tell the owner what was published, with the PyPI link, the image digest and the release link, and list anything not done.

## If something goes wrong

- **The clean-checkout job fails:** fix it on the working branch and run the job again on the new commit. Do not tag a commit the job has not passed.
- **Hashes differ between machines:** do not upload. Find the difference first (a different commit, an uncommitted file, a different build tool version in `RELEASE.json`).
- **The upload succeeded and the image or signature failed:** the PyPI version stands. Finish the image and signature for the same version; do not re-upload.
- **A fault is found after publishing:** release a new patch version. Say on the faulty version's release page which version replaces it.
