# Publish-safety audit

Audited 2026-10-26. **Do not flip this repository's visibility with its current history. Publish a reviewed snapshot as fresh, squashed history after explicit approval.** Historical personal data and private identifiers remain even though this change removes machine and project details from the current tree. This task does not create a repository, change visibility, rewrite history, revoke tokens, or choose a license.

## Scope and method

The initial full-history inventory at source HEAD `f44198db959fa89a232938e0c757d869ad2255bd` covered **54 commits and 252 unique blobs**, traversing `git rev-list --all HEAD` and every file in every commit tree, including deleted/replaced files. Commit objects (author/committer metadata and messages) were scanned too. This covers all commits reachable from locally available branches, remote-tracking refs, tags and HEAD, rather than only the current checkout. It does not fetch remote-only refs, scan unreachable garbage, GitHub issues/releases/artifacts, the real Keychain, Slack, or runtime files outside Git. Recheck remote ref coverage and rerun against the final approved publish snapshot before publication.

Reproduce the redacted inventory from any directory:

```sh
python3 scripts/audit-history.py
```

The scanner checks Slack bot/user/app token shapes, common OpenAI/Anthropic/AWS/GitHub/Google API-key shapes, private-key headers, quoted credential/signing-secret assignments, Slack identifier shapes, home paths, IPv4 addresses, email addresses, and Slack CLI project UUIDs. A supplemental scan of all historical blobs for known personal name/project terms also informed this report; those terms are deliberately not embedded in the public scanner or report. Results contain only file paths, commit hashes, kinds and occurrence counts. No matched secret, identifier, address or personal path is reproduced here. Pattern scanning cannot prove the absence of arbitrary or obfuscated secrets.

## Historical findings

| File or object | Example commit | Kind / assessment |
| --- | --- | --- |
| `.slack/config.json` | `4cfb7698e259ad3e5c06be3359b014a118fa2389` | Machine-generated Slack CLI project UUID; private identifier, replaced in current config by a freshly generated neutral UUID |
| `SETUP.md` | `7038549299ce85e1897a641ce6a94c913d1abd35` | Slack member-ID-shaped value outside fixtures; treat as private |
| `README.md` | `7038549299ce85e1897a641ce6a94c913d1abd35` | Personal user/home paths and project references |
| `slack_bridge/config.py` | `7038549299ce85e1897a641ce6a94c913d1abd35` | Personal Firstmate home path formerly used as a runtime default |
| `README.md` | `a18e7c8f5402793194d1a2b60204e8269cc5f160` | Private network/IPv4 reference; removed in later history |
| `README.md`, `slack_bridge/outbound.py`, `tests/test_topics.py` | `21bdf86147d7802ea32610651a584a5c922cadfa` | Personal project/task example labels |
| `SETUP.md`, `tests/test_channels.py` | `70edaed2160f432bceaedbc5cc738c369a5d02cf` | Personal project labels |
| `tests/test_images.py` | `a18e7c8f5402793194d1a2b60204e8269cc5f160` | Personal project labels; IPv4 matches here are loopback fixtures |
| Commit metadata/messages | `95b6acc1d4aaa07dabb2e5cb83452f936ba4faa8` | Author/committer personal name/email metadata throughout history |
| `tests/test_guard.py` | `27fd014128c94af822aaf693d59f4fabee6aedee` | Synthetic user-home path fixture; generalized in this change |

**Private data/identifiers in history: yes.** A clean HEAD does not remove these values from old commits. Retaining this history while making the repository public would expose them.

**No live credential was identified by this scan.** Slack-token-shaped matches were limited to synthetic fixtures in `tests/test_attachments.py` (example commit `73037a27848c09443b3d8b20bdc4bf5cb66b892f`), `tests/test_deploy_hook.py` (`4cfb7698e259ad3e5c06be3359b014a118fa2389`) and `tests/test_setup.py` (`f1abc9ae95ecb83c6acaa52bbb05372a46f72811`). Their source and use in offline fakes identify them as test data, not usable credentials. Identifier-shaped fixtures in `tests/helpers.py`, `tests/test_deploy_hook.py` and `tests/test_setup.py` are similarly synthetic. No API-key, private-key, or populated signing-secret candidate was found by the listed patterns. If an additional credential is later discovered, revoke/rotate it before publication; fresh history alone does not revoke it.

## Current tree and deliberate exceptions

- Runtime home paths derive from the current user's home, a saved wizard choice, or environment configuration. Setup captures the workspace/member and tokens rather than shipping private Slack IDs. The old machine-generated Slack CLI project UUID is replaced by a freshly generated, neutral one, committed so the Slack CLI does not rewrite the tracked config and block installer updates; the scanner still lists it as a `slack-project-uuid` match. Generated `.slack/apps*.json`, cache, runtime files, venv and environment files are ignored.
- Personal project/task names have been replaced in docs, source examples and fixtures. The test DM no longer includes the Mac's hostname. Synthetic Slack IDs and token-shaped strings remain where offline API/validation tests require their syntax; none selects a real workspace or account.
- Loopback addresses in URL tests are intentionally retained: they are generic local-network behavior fixtures, not private machine addresses. Synthetic paths, process names, fake panes and fictional member IDs likewise describe test inputs.
- The GitHub owner/repository segment remains in the default clone URL and documented raw installer URL. It is the existing distribution locator, not a runtime identity or private account configuration. If approval selects a new public repository for fresh history, update those URLs before publication. Other standard service labels and defaults (`com.firstmate.slack-bridge`, `default` Herdr session, `~/Documents/firstmate`) are configurable product conventions, not machine bindings.
- This version supports one bridge/workspace per macOS account because the service label and Keychain names are fixed. A different work Mac/account gets its own setup. Workspace binding checks (including verification of older credentials) stop accidental reuse; replacement is an explicit documented uninstall/revoke/reinstall procedure.

## Publication decisions still open

1. Obtain explicit approval for the final public repository and visibility change. Recommended route: a new repository with one reviewed, sanitized snapshot commit and reviewed author metadata. Do not copy old refs/tags, private runtime data or `.slack/apps*.json` into it. No history rewrite has been performed here.
2. Choose a license. **No LICENSE file has been added.** Public visibility does not itself grant an open-source license; the owner must choose one before describing this as open source.
3. Confirm the chosen repository URLs in `scripts/install.sh`, README and SETUP, and run the documented installer on a fresh Mac/account and target workspace after approval. Current validation is offline using temporary homes, real temporary Git repositories and fake Slack/Keychain/launchd/Herdr; no live workspace installation or work-policy certification is claimed.
