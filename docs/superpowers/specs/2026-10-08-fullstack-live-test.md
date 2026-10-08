# Full-stack live test, 2026-10-08

The six families had each been live-tested alone. This run migrated **all of them at once**, which
is the only way the seams between them get exercised.

## Source

The whole `testbed/aws` (74 resources) in `ap-south-1`: VPC with public and private subnets, NAT
gateway, custom NACL, four security groups including two shared across families, two RDS instances
(public Postgres, private MySQL), an EKS cluster with a managed node group, an Auto Scaling group
with a launch template, three S3 buckets (public website, logs target, Object Lock), standalone and
stopped EC2 instances, an external ALB with a path rule and an ASG target group, and an internal ALB.

**Not included: a Network Load Balancer.** The account is refused by AWS
(`OperationNotPermitted: This AWS account currently does not support creating load balancers`),
which applies to NLBs; ALBs create normally. Confirmed again on 2026-10-08 in the default VPC. NLB
coverage therefore remains goldens, provider-schema validation, and the synthetic snapshot entry of
2026-09-22. Lifting it needs AWS Support.

## Result

`scan` → 16 IR kinds, 6 families, in one document. `analyze` → 100 differences
(VM 38, DB 19, S3 14, LB 13, K8S 12, NET 4). `plan` → 59 resources. **`apply` succeeded first
time**, and a second `plan` reported **no changes** — no drift anywhere.

Cross-family seams verified in the generated Terraform and then live:

- The MySQL security group, shared by an RDS instance and an EC2 instance, survived as firewall
  rules — the latent bug spec 4 §2.1 was written to fix, now proven in the configuration that has
  both families present.
- The app VM wears both of its security groups' tags; the stopped VM wears the db group's; the GKE
  node pool wears the EKS cluster security group's tag.
- `NET-EGRESS-TAG` narrowed the app→db egress rule to `10.0.10.0/24`, the private subnet hosting
  the db-group instance, not the whole VPC.
- The external ALB used a zonal NEG for its instance target group and the **managed instance group**
  for its ASG target group; the internal ALB used a second NEG over the same instance — the shape
  the 2026-09-22 balancing-mode fix exists to produce.
- Zone mapping agreed across instances, the MIG and the node pool.

Live checks: `curl` to the external ALB returned the migrated VM's page; the internal ALB answered
from inside the VPC through its proxy-only subnet; Cloud SQL's private IP accepted TCP on 3306 from
the migrated VM; both GKE nodes registered `Ready` on 1.36.4-gke; the stopped VM arrived
`TERMINATED`; all three buckets were created with their configuration.

Two behaviours worth stating, both correct:

1. **The public Postgres instance was not migrated.** `DB-PUBLIC-ACCESS` made it `manual` because
   its security group's source is another security group, which would produce an unreachable public
   Cloud SQL (spec 2 §8b). The report lists it under "Needs your action" with the five decisions
   that no longer apply; nothing was silently dropped.
2. **`/asg/*` through the ALB returns 503.** The ASG's launch template runs no web server, so that
   target group is **unhealthy in AWS too** (`Target.FailedHealthChecks`). GCP reports `UNHEALTHY`
   for the same backend: the migration reproduced the source faithfully, including its breakage.

The Cloud Armor quota preflight added on 2026-09-22 fired before anything touched GCP
(`SECURITY_POLICIES` limit 0 on the test project), and the run continued with
`LB-SOURCE-RESTRICTION` set to `open`.

## Teardown

Both clouds destroyed. The Cloud SQL `google_service_networking_connection` again blocked
`tofu destroy` until the VPC peering was deleted by hand — the documented workaround, still needed,
still worth automating.

## Follow-ups

- Automate the service-networking peering deletion in teardown, or document it in the UI.
- `ec2:network-interface` noise: EKS-created ENIs still appear as unsupported resources.
- Untested live anywhere: NLB (account-restricted), HTTPS managed certificates (needs a domain),
  Fargate, self-managed node groups, Windows nodes.
