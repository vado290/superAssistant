## This task creates a project: the requirements document is the new project's charter (a format Kernel relies on)
This task has only the requirements stage: you agree the new project's charter with the user, and once the user
approves it on a card, Kernel **creates the project automatically** from it (project folder, charter, PM/Supervisor
configuration, skills, Discord channels) — no design or build. So this task's task_contract is not an ordinary
requirements document but follows this structure (headings exactly as written here, in English; the content in the
user's language):

```
# <project name> · Project charter
## 1 {{heading:purpose}}
## 2 {{heading:goals}}
## 3 {{heading:users}}
## 4 {{heading:background}}
## 5 {{heading:scope}}            (in scope / out of scope)
## 6 {{heading:constraints}}
## 7 {{heading:routing}}        (aliases; 3–5 typical requests, written the way the user would say them; requests it does not handle)
## 8 {{heading:archive}}    (organisation and industry facts collected now and reusable later, each with source and date; "None" (or the word for it in the user's language) if there are none. Kernel stores it in the new project's knowledge/background.md)
## {{heading:creation}}
## {{heading:profile}}
## {{heading:revision_log}}
```

"{{heading:constraints}}" may briefly name known long-term delivery locations, existing repositories or granted
directories, data retention requirements and important exceptions; it adds no required section or creation parameter
and does not ask for a full directory tree when the project is created. The detailed structure is for later design and
lives in the map or the project's documents; naming a location does not mean the directory exists or access is granted.

The "{{heading:creation}}" section holds a single YAML code block that Kernel parses directly (the sections
"{{heading:creation}}", "{{heading:profile}}" and "{{heading:revision_log}}" are not written into the new project's
charter; "{{heading:archive}}" goes into the new project's knowledge/background.md):

```yaml
project_id: sales-report        # 3–40 lowercase letters, digits or hyphens, starting with a letter; must not repeat an existing project
display_name: Sales report
purpose: one sentence on what the project is for
project_kind: business         # business (default) or chief_of_staff; kernel is reserved
capabilities: []                # optional: routing, project_management, shared_context; ordinary projects leave it empty
delivery: vault-files           # code projects use git-repo (Kernel creates a git repository in the project folder; builds happen on their own branch and are fast-forwarded after approval)
share_with_kernel: improvements # metrics / improvements / full: what operating information kernel may summarise from this project
project_access:                  # optional project-level cross-project reads; read-only only, self is the default
  mode: self                     # self | specific | all | all_except
  targets: []                    # for specific: exact ready Project IDs
  excluded: []                  # for all_except: Projects excluded from the current set
  include_future: false         # for all/all_except: include Projects that become ready later
  read_scopes: [catalog, summary, tasks, history, documents]
  expires_at: null              # optional absolute ISO-8601 expiry for the base policy
directories:                    # optional: outside directories the project needs (only under H:\AI; approving the charter grants them)
  - {path: /home/v/AI/crypto, access: read, purpose: read the strategy code}   # container path (H:\AI is /home/v/AI); access: read / write
pm_role: |
  project-specific notes for the PM (a few lines, may be empty)
supervisor_role: |
  project-specific notes for the Supervisor (a few lines, may be empty)
```

- For a user-requested Chief of Staff project, choose project_kind: chief_of_staff. Its PM collects evidence and
  prepares advice; its Supervisor challenges assumptions and checks alignment with the user's stated goals. P0 allows
  vault-files, no outside directory grants, and at most project_management as an optional capability. It has no special
  cross-project permission: use an explicitly approved project_access policy only. Do not enable routing/shared_context
  or kernel_maintenance. Bootstrap creates its own Discord category and #pm / #supervisor entry points as usual.
- After every save Kernel checks the creation parameters and shows the result at the end of the document
  ("Kernel check: …"); fix what it reports.
- The industry research (domain_brief) is written into the new project's `knowledge/domain-brief.md` when the project
  is created; "{{heading:profile}}" goes into the new project's user profile.
- When the charter is done, use outcome completed; the Supervisor asks the user for approval — you need not.
