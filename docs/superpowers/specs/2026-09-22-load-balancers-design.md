# EasyMigrate — Load balancers: ALB / NLB → Cloud Load Balancing (Spec 7)

Status: draft 2026-09-22. Design calls delegated; §9 records each with its reason.
Builds on spec 4 (instances and managed instance groups are the backends) and spec 1 (firewall).

## 1. Scope

Sixth family. Elastic Load Balancing v2 **Application** and **Network** load balancers whose targets
are EC2 instances or Auto Scaling groups migrated by spec 4.

### In scope

Load balancers (internet-facing and internal), listeners (HTTP, HTTPS, TCP, UDP, TCP_UDP), listener
rules routing by host and path to forward or redirect actions, target groups of target type
`instance` with their health checks, stickiness and deregistration delay, TLS certificates (as
Google-managed certificates), SSL policies, the ALB security group's source restriction (as a Cloud
Armor allow-list), access logging and idle timeout.

### Out of scope

- **Classic Load Balancers** and **Gateway Load Balancers**: reported as unsupported.
- Target types `ip`, `lambda` and `alb` (named, their target groups not generated).
- Load balancers created by the AWS Load Balancer Controller for Kubernetes Services/Ingresses
  (tagged `elbv2.k8s.aws/cluster`): they belong to workloads, which spec 5 does not migrate.
- Authenticate (OIDC/Cognito) and fixed-response actions, header/query/method/source-IP
  conditions, AWS WAF associations, mTLS: named differences, not translated.
- DNS. The migrated load balancer has a new IP; repointing DNS is the user's cutover step.

## 2. Changes outside the family

- **Compute exports** `instance_tf_labels(resolved)` and `group_tf_labels(resolved)` (like the
  network family's label exports) and renders `named_port` blocks on a managed instance group from a
  `named_ports` hint, which `LB-BACKENDS` writes.
- **`VM-ASG-TARGET-GROUPS`** stops saying the attachment is lost: when the target group's load
  balancer is in the scan, the group becomes its backend; the effect text says so.
- `LoadBalancingPlugin.requires = {"network", "compute"}` (targets resolve to compute objects).
  `PLUGINS = (Network, Compute, LoadBalancing, Kubernetes, Storage, Database)`.

## 3. Collection

`elbv2`: `DescribeLoadBalancers`, `DescribeLoadBalancerAttributes`, `DescribeListeners`,
`DescribeRules`, `DescribeTargetGroups`, `DescribeTargetGroupAttributes`, `DescribeTargetHealth`,
`DescribeTags`; `acm:DescribeCertificate` for listener certificates; `ec2:DescribeInstances` for
target instances (to learn Auto Scaling membership); classic `elb:DescribeLoadBalancers` for the
unsupported inventory. Snapshot files `loadbalancing.*.json`. Denied calls are scan gaps.

## 4. IR

`load_balancer`: `lb_type` (`application`|`network`), `scheme` (`internet-facing`|`internal`),
`subnets`, `firewall_policies` (ALB security groups), `listeners` (embedded: port, protocol,
certificates `[{arn, domains}]`, `ssl_policy`, `default_action`, `rules` `[{priority, hosts, paths,
unsupported_conditions, action}]`), `idle_timeout`, `access_logs`, `deletion_protection`,
`kubernetes_owned`. An action is `{type: forward|redirect|fixed-response|authenticate,
target_groups: [{target_group, weight}], redirect: {protocol, port, host, path, status}}`.

`target_group`: `protocol`, `port`, `target_type`, `network`, `health_check` (`protocol, port, path,
interval, timeout, healthy, unhealthy, matcher`), `stickiness` (`lb_cookie`|`app_cookie` + cookie
name + duration), `deregistration_delay`, `instances` (compute instance refs), `groups` (instance
group refs, from target instances' Auto Scaling membership), `unresolved_targets`.

References: listener actions → target groups; target groups → instances, groups, network.

## 5. Difference catalogue (`LB-*`)

| Rule | Detects | Strategies (**default** first) |
|---|---|---|
| `LB-SHAPE` | every LB | **`map`**: internet-facing ALB → global external Application LB; internal ALB → regional internal Application LB (+ a proxy-only subnet, allocated from `100.126.0.0/16`); internet-facing NLB → regional external passthrough Network LB; internal NLB → internal passthrough Network LB. Risk low; medium for internal ALBs (new proxy subnet, backends see proxy source IPs) |
| `LB-NEW-ADDRESS` | every LB | **`reserve_ip`** (high): a new static IP replaces the AWS DNS name (and any NLB Elastic IPs); clients and DNS must be repointed at cutover |
| `LB-BACKENDS` | every target group | **`instance_groups`** (low): standalone instances join one unmanaged instance group per zone (an instance can be in only one load-balanced group, so groups are shared across target groups, with one named port per target group); Auto Scaling targets use their managed instance group. Target groups of type `ip`/`lambda`/`alb` → **`manual`** only, not generated, and actions forwarding only to them are dropped |
| `LB-CERTIFICATES` | HTTPS/TLS listener | **`google_managed`** (medium): a Google-managed certificate for the ACM certificate's domains; it provisions only after DNS points at the new IP. Wildcard domains → **`manual`** only (bring your own certificate) |
| `LB-SSL-POLICY` | HTTPS listener | **`map_policy`** (low): TLS 1.2+ AWS policies → `MODERN`/`TLS_1_2`; TLS 1.3-only → `RESTRICTED`/`TLS_1_2` (closest), others → `COMPATIBLE` |
| `LB-SOURCE-RESTRICTION` | internet-facing ALB whose security groups admit less than the world | **`cloud_armor_allowlist`** (low): a Cloud Armor policy allowing only those CIDRs · `open` (high) |
| `LB-RULE-CONDITIONS` | rules with header/query/method/source-IP conditions, or path patterns with an inner wildcard | **`drop_rule`** (medium): the rule is not generated and is named |
| `LB-UNSUPPORTED-ACTIONS` | fixed-response or authenticate actions | **`drop_rule`** (medium; high when the default action): a default fixed-response becomes the lowest-priority forward target |
| `LB-NLB-PORT-TRANSLATION` | NLB listener port ≠ target port | **`manual`**: a passthrough LB cannot translate ports; that listener is not generated |
| `LB-HEALTH-CHECK` | matcher other than `200`, or an HTTP check on a TCP target group | **`map_health_check`** (low; medium when the matcher is not 200): GCP treats only 200 as healthy |
| `LB-STICKINESS` | stickiness enabled | **`cookie_affinity`** (low): `lb_cookie` → `GENERATED_COOKIE`, `app_cookie` → `HTTP_COOKIE` |
| `LB-KUBERNETES-OWNED` | LB created by the AWS Load Balancer Controller | **`exclude`** (low): not generated; GKE Services and Ingresses recreate it with the workloads |
| `LB-ACCESS-LOGS` | access logs enabled | **`cloud_logging`** (low): request logging on the backend services |

Also recorded: `deletion_protection` (reported), `idle_timeout` → backend `timeout_sec`.

## 6. Generation (`terraform/loadbalancing.tf`)

- Global external ALB: `google_compute_global_address`, `google_compute_health_check`,
  `google_compute_backend_service` (`EXTERNAL_MANAGED`, `port_name`, `session_affinity`,
  `connection_draining_timeout_sec`, `timeout_sec`, `security_policy`, `log_config`), one
  `google_compute_url_map` per listener (host rules/path matchers, or `default_url_redirect`),
  `google_compute_target_http(s)_proxy`, `google_compute_managed_ssl_certificate`,
  `google_compute_ssl_policy`, `google_compute_global_forwarding_rule`,
  `google_compute_security_policy`.
- Regional internal ALB: the `region_` equivalents, the proxy-only subnet
  (`purpose = "REGIONAL_MANAGED_PROXY"`), forwarding rule `INTERNAL_MANAGED`.
- Passthrough NLB: `google_compute_region_health_check`, `google_compute_region_backend_service`
  (`EXTERNAL`/`INTERNAL`, `balancing_mode = CONNECTION`), `google_compute_address` (external),
  `google_compute_forwarding_rule` (`ports`).
- Backends: `google_compute_instance_group` per zone (instances by compute's labels, named ports),
  or the MIG's `instance_group`.
- Firewall: health-check ranges (and, for proxy LBs, Google front-end or proxy-subnet ranges) to the
  backends' network tags on the target ports.

## 7. Report

`## Load balancers`: per LB the GCP type, new address, listeners and where each routes, what was
dropped, and the DNS cutover note.

## 8. Testing

Rule unit tests; moto collection; normalization; goldens (external ALB with HTTPS + redirect + path
rules + ASG backend, internal ALB, external NLB, internal NLB, k8s-owned); provider schema; live run
(external ALB on HTTP with a path rule, external NLB on TCP, both reaching a migrated VM).

## 9. Decisions taken (delegated 2026-09-22)

- **D1 Global external ALB for internet-facing ALBs.** No proxy-only subnet, one anycast IP, TLS at
  Google's edge; the regional external variant adds a subnet and gains nothing for one region.
- **D2 Passthrough NLBs.** NLBs preserve client IPs and pass TCP through; the passthrough Network LB
  does the same. Port translation is the one thing it cannot do, which is named.
- **D3 Google-managed certificates.** ACM certificates cannot be exported; managed certificates are
  the only automatic path, at the cost of provisioning after DNS moves.
- **D4 Cloud Armor for ALB source restrictions.** A global external ALB has no security group; an
  allow-list policy is the equivalent that keeps a restricted ALB restricted.
- **D5 Endpoint groups for ALBs, one unmanaged instance group per zone for NLBs.** GCP allows an
  instance in only one load-balanced instance group, and one group cannot serve both balancing
  modes (amended after the live run).
- **D6 Kubernetes-controller LBs excluded**, consistent with spec 5's empty-cluster boundary.

## 10. Plans

1. IR, collection, normalization, compute exports.
2. `LB-*` rules.
3. Generation, report, goldens, provider schema.
4. Testbed and live run.

## 11. Live run, 2026-09-22

**Partly synthetic, stated plainly.** The AWS account refused to create a Network Load Balancer
(`OperationNotPermitted: This AWS account currently does not support creating load balancers`);
the ALB was created. So the ALB, both target groups and the app instance went through the real
AWS collection, and one NLB entry, shaped like `DescribeLoadBalancers` output and pointing at the
real SSH target group, was added to the snapshot before `analyze`. Everything from `analyze`
onward, including `apply` to GCP, is real.

Proven on GCP (`asia-south1`): the global external ALB answered with the migrated VM's page;
`/api/x` reached the backend through the path rule (404 from the VM's own server); SSH through the
external passthrough NLB logged into the same VM; a second `plan` showed no changes; teardown
clean.

Found and fixed:
1. **Cloud Armor quota.** The project's `SECURITY_POLICIES` limit was 0, so the default allow-list
   failed at apply. `plan` now reads the quota first and stops with the two ways out.
2. **One instance group cannot serve an ALB and an NLB** (`incompatible balancing_mode`), and an
   instance can be in only one load-balanced instance group. Application LB backends now use zonal
   network endpoint groups (`GCE_VM_IP_PORT`); Network LBs keep unmanaged instance groups. D5 is
   amended accordingly; a managed instance group behind both kinds is a `GenerationError`.

Not proven live: HTTPS (needs a domain for the managed certificate), internal ALB/NLB, ASG backends
— covered by goldens and provider-schema tests. (Internal ALB and ASG backends were proven in the
2026-10-08 full-stack test; HTTPS on 2026-10-27, below.)

### HTTPS live test (2026-10-27)

No owned domain needed: sslip.io resolves `<a-b-c-d>.sslip.io` to `a.b.c.d`. The testbed ALB got an
HTTPS listener (`ELBSecurityPolicy-TLS13-1-2-2021-06`) with a self-signed certificate imported into
ACM (`TF_VAR_https_domain`). Round 1 migrated it with a placeholder domain to learn the new global
address (136.81.238.39); round 2 re-issued the ACM certificate for `136-81-238-39.sslip.io`, and
scan → analyze → resolve → plan replaced only the managed certificate. LB-SOURCE-RESTRICTION was
set to `open`: Google's certificate validation must reach the load balancer, and the project has
no Cloud Armor quota.

- **Found and fixed:** the replacement failed, because GCP refuses to delete a certificate a
  proxy still uses (`resourceInUseByAnotherResource`). Certificates are now named after a hash
  of their domains with `create_before_destroy`; the re-plan created the new one, moved the
  proxy and deleted the old one in a single apply.
- The certificate went `ACTIVE` about 35 minutes after the apply. `curl https://136-81-238-39.sslip.io/`
  then verified a Google Trust Services certificate over TLS 1.3 and returned the migrated VM's
  page; TLS 1.0/1.1 were refused and TLS 1.2 accepted (`MODERN`, minimum 1.2, from the AWS
  policy). A second `plan` showed no changes; `easymigrate destroy` removed all 22 resources.

Still synthetic: the AWS side of an NLB. The account refuses to create NLBs in every region
(OperationNotPermitted, re-checked 2026-10-27) while ALBs work; AWS Support must lift it.
