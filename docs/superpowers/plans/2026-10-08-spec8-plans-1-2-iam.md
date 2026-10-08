# Spec 8, Plans 1-2: IAM collection and report

> Lean plan. Spec §2-§6.

- `ir/iam.py` (`iam_role`), IAM plugin (global; generates nothing), Kubernetes cluster/node role capture.
- Collection: one `GetAccountAuthorizationDetails`, plus default documents of AWS-managed policies attached to roles; users reduced to names and groups (no credentials).
- Normalization: per-service read/write/admin by action verb (Receive counts as read), wildcard-resource and admin-everything flags, trust principals, service-linked roles skipped, account summary finding.
- `iam_facts.py` service->predefined-role suggestions; report `## Identity and access` links each role to the migrated objects using it (instance profiles, EKS cluster/node roles, pod identities, IRSA via OIDC issuer).
