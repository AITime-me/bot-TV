# Controlled deployment: fenced amoCRM on-demand writer

This runbook deploys only the disposable `controlled-revision` image.  It does
not recreate or restart API, worker, database, Chat, or any queue consumer.
It is intentionally a plan until a technical test lead and its automation
isolation are proven read-only.

## Recorded safety gate (5 September 2026)

The initial read-only inventory found no active `TECHNICAL_DEAL` link that can
serve as a proof target. Salesbot inventory was empty. Two enabled account
webhooks were traced to their receivers:

- the Salebot receiver subscribes only to `add_unsorted`, so a PATCH or stage
  update would not invoke it;
- the Wazzup receiver is external to this contour and subscribes to
  `add_lead`, `update_lead`, `status_lead`, `responsible_lead`,
  `delete_lead`, and `restore_lead` (as well as contact/company events).

The Wazzup subscription can notify its external receiver for a field change or
a stage change, independently of local queue state. Its receiver logic and any
filtering are not controlled by bot-TV or observable through the amoCRM API.
Consequently this runbook is **not executable** yet: do not create a substitute
lead or apply any plan until an owner-designated technical object and a
documented Wazzup exclusion (and any applicable Digital Pipeline automation)
are available.

The required future authority is one controlled production decision covering:

1. the exact isolated technical lead and its original reversible value;
2. evidence that the applicable Wazzup webhook and pipeline automation will
   not run;
3. merge/deployment of PR #100's disposable profile; and
4. one dry-run, one single-action apply, independent read-back, and one
   separately reviewed inverse action.

## Preconditions

1. PR #100 is merged and the exact merged source is available on the production
   host. Record both the deployed commit and the previous commit.
2. Fresh read-only checks confirm:
   - `AMOCRM_CHAT_EGRESS_ENABLED=false` and
     `AMOCRM_CRM_DEAL_CREATE_ENABLED=false` for API and worker;
   - `outbox_messages`, `amocrm_mirror_jobs`, and
     `amocrm_message_projections` have no sendable row;
   - the designated technical lead is real, uniquely identified, and is not a
     client record;
   - its pipeline/status has no automation that sends a message, creates a
     business task, changes a client record, or calls an external service.
3. The proof action is an allowlisted reversible field/stage update agreed for
   that technical lead. Do not use lead/task creation as the first live proof.

If the last two conditions cannot be established from read-only evidence, stop
before `--apply`.  A local empty queue does not prove amoCRM-side automation is
absent.

## Exact deployment sequence

Run under the existing production deployment account from the bot-TV repository:

```bash
set -euo pipefail
previous_commit="$(git rev-parse HEAD)"
git fetch origin
git switch main
git pull --ff-only origin main
candidate_commit="$(git rev-parse HEAD)"
test "$candidate_commit" != "$previous_commit"

docker compose --profile controlled-revision build controlled-revision
docker compose --profile controlled-revision run --rm -T \
  controlled-revision fenced-on-demand-write < /run/amo-proof-plan.json
```

The second command is a mandatory dry run: it has no `--apply` and must return
`DRY_RUN`.  The plan file is root/operator-created with mode `0600`, contains no
OAuth secret, and has exactly one allowlisted action.  Review its returned
entity id and operation before the write step.

Only after all preconditions and the dry-run result are recorded, execute the
same disposable container with both independent write gates:

```bash
AMOCRM_CRM_ON_DEMAND_WRITE_ENABLED=true \
docker compose --profile controlled-revision run --rm -T \
  -e AMOCRM_CRM_ON_DEMAND_WRITE_ENABLED=true \
  controlled-revision fenced-on-demand-write --apply < /run/amo-proof-plan.json
```

The process exits after one plan.  It has no port, restart policy, scheduler,
queue consumer, API command, worker command, or Chat import.

## Post-deploy proof

Record only identifiers and outcomes, never CRM payloads or tokens:

1. The CLI returns `APPLIED` and its built-in GET → PATCH → GET postcheck
   succeeds.
2. Independent existing OAuth REST GET re-reads the technical lead and confirms
   the exact intended field/stage only.
3. Fresh aggregate queue check remains zero for sendable outbox/mirror/chat
   rows; API/worker remain healthy and retain their disabled write/egress flags.
4. Inspect relevant local logs for errors without printing client payloads.
5. Restore the technical record's original value using a second separately
   reviewed one-action plan, then repeat steps 1–4.  If a stage move cannot be
   reversed without business automation, do not use it as a proof action.

## Rollback

No running production service is changed by building or running this profile.
For a failed build or dry-run, stop: no rollback is required.  For a failed
postcheck after an applied technical update, do not retry automatically.  Use a
fresh GET and the inverse one-action plan only when its automation safety is
already proven; otherwise preserve the evidence and request a dedicated
recovery decision.

If the disposable image itself must be removed, return the checkout to
`$previous_commit` and rebuild only `controlled-revision`; do not restart
API/worker/database or run `docker compose down`.
