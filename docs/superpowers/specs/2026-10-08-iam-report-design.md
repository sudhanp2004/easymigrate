# EasyMigrate — IAM: report only (Spec 8)

Status: draft 2026-10-08. Design calls delegated; §7 records each with its reason.

## 1. Scope

The roadmap's last family: **IAM is reported, never translated.** Nothing is generated. The report
says which IAM roles the migrated resources use, what each could do in AWS, and the Google Cloud
predefined roles that are the nearest starting point — so the user can build service accounts and
bindings deliberately rather than discover missing permissions at cutover.

### In scope

- Roles: trust (which AWS services or OIDC providers may assume them), instance profiles, inline
  and managed policies, and the access they grant, summarised per AWS service as read / write / admin.
- Which migrated objects use each role: EC2 instances and launch templates (instance profiles),
  EKS clusters and node groups (cluster and node roles), EKS pod identities and IRSA (OIDC trust).
- Account-level counts of users, groups and roles, with the statement that people's access is
  re-granted through Google Cloud IAM.

### Out of scope

- Creating service accounts, bindings, custom roles or Workload Identity bindings.
- Evaluating policy conditions, permission boundaries, SCPs, resource policies, session policies.
- Users' passwords, access keys or MFA state: a security audit is not a migration report.

## 2. Architecture

```
ir/iam.py                                  iam_role
plugins/iam/   collect_aws.py normalize.py generate_gcp.py (generates nothing) __init__.py
differences/rules/aws_gcp/iam_facts.py     action → access level; AWS service → GCP roles (dated)
core/report.py                             "## Identity and access"
```

`IamPlugin.requires = frozenset()`; IAM is global, so the region does not filter it.
`PLUGINS` gains it last. `aws_resource_types = {"iam:role", "iam:user", "iam:group", "iam:policy",
"iam:instance-profile", "iam:oidc-provider"}`.

Small capture additions elsewhere so roles can be linked: the Kubernetes normalizer records the
cluster's `roleArn` and each managed node group's `nodeRole` (new optional fields `role_arn` on
`kubernetes_cluster` and `node_pool`, recorded only).

## 3. Collection

`iam:GetAccountAuthorizationDetails` with `Filter = [User, Group, Role, LocalManagedPolicy]`
(one paginated call carries every entity and customer-managed policy document). For AWS-managed
policies attached to roles, `GetPolicy` + `GetPolicyVersion` fetch the default document — only for
policies actually attached, never the thousand-plus catalogue. Denied → scan gap.

Snapshot: `iam.authorization.json` (roles, users and groups reduced to names, group membership and
attached policy names — no credentials), `iam.aws_managed_policies.json`.

## 4. IR — `iam_role`

`arn`, `trusted_services` (e.g. `ec2.amazonaws.com`), `trusted_oidc` (provider ARNs),
`instance_profiles` (ARNs), `policies` (`[{name, kind: inline|customer|aws}]`), `access`
(`[{service, level: read|write|admin, actions_sample}]`, derived from Allow statements across all
the role's policies), `wildcard_resources` (any Allow on `"*"` resources), `admin_everything`
(an Allow of `"*"` actions). Users and groups become one `info` finding with counts, not objects.

## 5. Report — `## Identity and access`

Opening statement: no IAM was migrated. Then, for each role a migrated object uses:

- the role and what uses it (by IR id);
- per AWS service: access level, and the suggested Google Cloud predefined role(s);
- warnings: `admin_everything` ("do not copy this; grant per service"), `wildcard_resources`;
- how the identity attaches on GCP: a service account on the VM / node pool, or a Workload Identity
  binding for pod identities and IRSA.

Then: N other roles not used by anything migrated (named, not detailed), and user/group counts.

## 6. Testing

Unit: access-level classification, service → role mapping, linking (instance profile ARN, EKS
roles, OIDC trust), report section. moto collection. Live: scan the real account with a role +
instance profile on a test instance and an EKS cluster; check the report names them correctly.
No GCP resources are created by this family, so the live run costs only the AWS testbed.

## 7. Decisions taken (delegated 2026-10-08)

- **D1 No difference rules.** `VM-IAM-PROFILE` and `K8S-IAM-FOR-PODS` already make the user
  acknowledge every place IAM matters; a third acknowledgement per role would be noise.
- **D2 Predefined roles as suggestions, never generated.** Least-privilege needs knowledge of the
  workload that a scan does not have; a wrong generated binding is worse than a clear list.
- **D3 Classify by action verb.** `Get/List/Describe/Head/Select/Scan/Query/BatchGet` read; anything
  else writes; `service:*` or `*` is admin. Coarse, deterministic and explainable.
- **D4 No credentials in the snapshot.** Users are reduced to names and memberships; the snapshot
  sits in `.easymigrate/`, which `init` git-ignores, but it still must not hold secrets.

## 8. Plans

1. IR, collection, normalization, Kubernetes role capture.
2. `iam_facts.py` and the report section.
3. Live run.

## 9. Live run, 2026-10-08

The real account (538342414628) scanned with an instance role on the testbed app VM: an inline
policy (S3 read, SQS receive + delete, CloudWatch Logs write) and the AWS-managed
`AmazonSSMManagedInstanceCore`. `plan` only — this family creates nothing on GCP.

The report linked `migration-test-app-role` to `compute_instance:migration-test-app` through its
instance profile, as "a service account attached to the VM", and flagged its wildcard resources.
Two suggestions were wrong on the first run and are fixed, each with a regression test:

1. **A queue consumer was told it needed publisher rights.** `sqs:DeleteMessage` acknowledges a
   received message; it now counts as consuming, so the suggestion is `roles/pubsub.subscriber`.
2. **The Systems Manager agent was mapped to Secret Manager.** `AmazonSSMManagedInstanceCore` is
   Session Manager's agent policy; it shares the `ssm` prefix with Parameter Store and nothing else.
   `ssm` now splits into `ssm-parameters` (→ Secret Manager) and `ssm-agent` (with `ssmmessages`
   and `ec2messages`), which the report describes as needing no role — use OS Login with IAP.

Not live-tested: EKS cluster, node and pod-identity linking (covered by unit tests; the full-stack
run earlier the same day predates this family).
