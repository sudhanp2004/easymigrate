"""AWS→GCP load-balancing difference rules (spec 7 §5).

Rules decide the Cloud Load Balancing shape and record it in `target_hints`;
`plugins/loadbalancing/generate_gcp.py` renders it.
"""

from __future__ import annotations

import ipaddress
from typing import Any

from easymigrate.differences.model import Difference, DifferenceRule, Risk, Strategy
from easymigrate.differences.rules.aws_gcp.compute import (
    _AcknowledgeOnly,
    _replace_object,
    _set_hint,
    _subject,
)
from easymigrate.ir.base import IRDocument
from easymigrate.ir.loadbalancing import LoadBalancer, TargetGroup
from easymigrate.ir.network import CidrPeer, FirewallPolicy, Network, Subnet

_WORLD = {"0.0.0.0/0", "::/0"}
_PROXY_POOL = ipaddress.ip_network("100.126.0.0/16")
_PROXY_PREFIX = 23


def _lbs(ir: IRDocument) -> list[LoadBalancer]:
    return sorted(ir.of_kind(LoadBalancer), key=lambda o: o.id)


def _tgs(ir: IRDocument) -> list[TargetGroup]:
    return sorted(ir.of_kind(TargetGroup), key=lambda o: o.id)


def _slug(ir_id: str) -> str:
    return ir_id.split(":", 1)[1]


def _shape(lb: LoadBalancer) -> str:
    if lb.lb_type == "application":
        return "global_external_alb" if lb.scheme == "internet-facing" else "regional_internal_alb"
    return (
        "external_passthrough_nlb" if lb.scheme == "internet-facing" else "internal_passthrough_nlb"
    )


class LbKubernetesOwned(DifferenceRule):
    id = "LB-KUBERNETES-OWNED"
    family = "loadbalancing"
    title = "Load balancers created for Kubernetes are left to the cluster"
    impact = (
        "The AWS Load Balancer Controller made this load balancer for a Kubernetes Service or "
        "Ingress. It belongs to the workload, which is not migrated; a GKE Service or Ingress "
        "recreates it once the workload is redeployed."
    )
    strategies = (
        Strategy(
            id="exclude",
            title="Leave it to the cluster",
            effect="nothing is generated; redeploying the workload on GKE recreates it",
            risk="low",
        ),
    )
    default = "exclude"

    def detect(self, ir: IRDocument) -> list[Difference]:
        return [self.difference([lb.id]) for lb in _lbs(ir) if lb.kubernetes_owned]

    def apply(self, ir, diff, strategy_id, params=None) -> IRDocument:
        doc, lb = _subject(ir, diff)
        if lb is None:
            return doc
        if strategy_id != "exclude":
            raise ValueError(f"unknown strategy {strategy_id!r}")
        return _replace_object(
            doc, lb.model_copy(update={"excluded": f"{self.id}: owned by Kubernetes"})
        )


def _occupied(ir: IRDocument) -> list[str]:
    ranges = [c for n in ir.of_kind(Network) for c in n.cidr_blocks]
    ranges += [s.cidr for s in ir.of_kind(Subnet)]
    ranges += [
        e["cidr"] for s in ir.of_kind(Subnet) for e in s.target_hints.get("secondary_ranges", [])
    ]
    return ranges


class LbShape(DifferenceRule):
    id = "LB-SHAPE"
    family = "loadbalancing"
    title = "Each load balancer maps to its closest Cloud Load Balancing type"
    impact = (
        "Internet-facing ALBs become global external Application Load Balancers, internal ALBs "
        "regional internal ones (which need a proxy-only subnet), and NLBs passthrough Network "
        "Load Balancers, which keep client source addresses as NLBs do."
    )
    strategies = (
        Strategy(
            id="map",
            title="Use the closest type",
            effect="the load balancer is recreated as the matching Cloud Load Balancing type",
            risk="low",
        ),
    )
    default = "map"

    def detect(self, ir: IRDocument) -> list[Difference]:
        taken = _occupied(ir)
        proxy_by_network: dict[str, str] = {}
        subnets = {s.id: s for s in ir.of_kind(Subnet)}
        diffs = []
        for lb in _lbs(ir):
            shape = _shape(lb)
            network = next((subnets[s].network for s in lb.subnets if s in subnets), None)
            proxy = None
            if shape == "regional_internal_alb" and network:
                if network not in proxy_by_network:
                    for candidate in _PROXY_POOL.subnets(new_prefix=_PROXY_PREFIX):
                        net = ipaddress.ip_network(candidate)
                        if not any(net.overlaps(ipaddress.ip_network(t)) for t in taken):
                            proxy_by_network[network] = str(candidate)
                            taken.append(str(candidate))
                            break
                proxy = proxy_by_network.get(network)
            diffs.append(
                self.difference(
                    [lb.id],
                    shape=shape,
                    network=network,
                    subnet=sorted(lb.subnets)[0] if lb.subnets else None,
                    proxy_subnet_cidr=proxy,
                )
            )
        return diffs

    def risk_for(self, diff: Difference, strategy_id: str) -> Risk:
        # A new proxy-only subnet, and backends see proxy addresses instead of client ones.
        return "medium" if diff.context.get("shape") == "regional_internal_alb" else "low"

    def apply(self, ir, diff, strategy_id, params=None) -> IRDocument:
        doc, lb = _subject(ir, diff)
        if lb is None:
            return doc
        if strategy_id != "map":
            raise ValueError(f"unknown strategy {strategy_id!r}")
        updated = _set_hint(lb, "shape", diff.context["shape"])
        updated = _set_hint(updated, "network", diff.context["network"])
        updated = _set_hint(updated, "subnet", diff.context["subnet"])
        if diff.context.get("proxy_subnet_cidr"):
            updated = _set_hint(updated, "proxy_subnet_cidr", diff.context["proxy_subnet_cidr"])
        return _replace_object(doc, updated)


class LbNewAddress(_AcknowledgeOnly, DifferenceRule):
    id = "LB-NEW-ADDRESS"
    family = "loadbalancing"
    title = "The load balancer gets a new address; DNS must follow"
    impact = (
        "AWS load balancers are reached through a DNS name (and NLBs sometimes through Elastic "
        "IPs). The migrated one has a new IP address. Nothing sends it traffic until DNS or "
        "clients are repointed, which is the cutover step EasyMigrate leaves to the user."
    )
    strategies = (
        Strategy(
            id="reserve_ip",
            title="Reserve a new address",
            effect="a static address is reserved; repoint DNS at it when you cut over",
            risk="high",
        ),
    )
    default = "reserve_ip"

    def detect(self, ir: IRDocument) -> list[Difference]:
        return [self.difference([lb.id]) for lb in _lbs(ir)]


class LbBackends(DifferenceRule):
    id = "LB-BACKENDS"
    family = "loadbalancing"
    title = "Target groups become backend services over instance groups"
    impact = (
        "A target group registers instances; a backend service names instance groups. Standalone "
        "instances join one unmanaged instance group per zone — GCP allows an instance in only one "
        "load-balanced group — and each target group is a named port on it. Auto Scaling targets "
        "use their managed instance group. Target types other than instance are not migrated."
    )
    strategies = (
        Strategy(
            id="instance_groups",
            title="Back it with instance groups",
            effect="the target group becomes a backend service over the migrated instances' groups",
            risk="low",
        ),
        Strategy(
            id="manual",
            title="Handle manually",
            effect=(
                "this target group is not generated, and actions forwarding only to it are dropped"
            ),
            risk="medium",
            manual=True,
        ),
    )
    default = "instance_groups"

    def detect(self, ir: IRDocument) -> list[Difference]:
        return [
            self.difference(
                [tg.id],
                target_type=tg.target_type,
                instances=tg.instances,
                groups=tg.groups,
                unresolved=tg.unresolved_targets,
            )
            for tg in _tgs(ir)
        ]

    def allowed_strategies(self, diff: Difference) -> tuple[Strategy, ...]:
        if diff.context["target_type"] != "instance":
            return (self.strategy("manual"),)
        return self.strategies

    def default_strategy(self, diff: Difference) -> str:
        return "manual" if diff.context["target_type"] != "instance" else self.default

    def risk_for(self, diff: Difference, strategy_id: str) -> Risk:
        if strategy_id == "instance_groups" and diff.context.get("unresolved"):
            return "medium"  # some registered targets are not in the migration
        return self.strategy(strategy_id).risk

    def apply(self, ir, diff, strategy_id, params=None) -> IRDocument:
        doc, tg = _subject(ir, diff)
        if tg is None:
            return doc
        if strategy_id not in {s.id for s in self.allowed_strategies(diff)}:
            raise ValueError(f"strategy {strategy_id!r} is not offered for {tg.id}")
        if strategy_id == "manual":
            return _replace_object(doc, tg.model_copy(update={"excluded": f"{self.id}: manual"}))
        named = {"name": f"tg-{_slug(tg.id)}"[:63], "port": tg.port or 80}
        doc = _replace_object(doc, _set_hint(tg, "named_port", named))
        for group_id in tg.groups:
            group = doc.get(group_id)
            ports = [
                p for p in group.target_hints.get("named_ports", []) if p["name"] != named["name"]
            ]
            doc = _replace_object(doc, _set_hint(group, "named_ports", [*ports, named]))
        return doc


class LbHealthCheck(DifferenceRule):
    id = "LB-HEALTH-CHECK"
    family = "loadbalancing"
    title = "Health checks carry, but only HTTP 200 counts as healthy"
    impact = (
        "A target group can treat a range of status codes as healthy. A Google Cloud health check "
        "passes only on 200, so a backend answering 204 or a redirect is marked unhealthy."
    )
    strategies = (
        Strategy(
            id="map_health_check",
            title="Map the health check",
            effect="protocol, port, path, interval, timeout and thresholds carry",
            risk="low",
        ),
    )
    default = "map_health_check"

    def detect(self, ir: IRDocument) -> list[Difference]:
        return [
            self.difference([tg.id], matcher=tg.health_check.matcher)
            for tg in _tgs(ir)
            if tg.target_type == "instance"
        ]

    def risk_for(self, diff: Difference, strategy_id: str) -> Risk:
        matcher = diff.context.get("matcher")
        return "medium" if matcher and matcher not in {"200", "200-200"} else "low"

    def apply(self, ir, diff, strategy_id, params=None) -> IRDocument:
        doc, tg = _subject(ir, diff)
        if tg is None:
            return doc
        if strategy_id != "map_health_check":
            raise ValueError(f"unknown strategy {strategy_id!r}")
        check = tg.health_check
        protocol = check.protocol.upper() if check.protocol.upper() in {"HTTP", "HTTPS"} else "TCP"
        port = tg.port if check.port == "traffic-port" else int(check.port)
        mapped = {
            "protocol": protocol,
            "port": port,
            "request_path": check.path if protocol != "TCP" else None,
            "check_interval_sec": check.interval,
            "timeout_sec": min(check.timeout, check.interval),
            "healthy_threshold": check.healthy,
            "unhealthy_threshold": check.unhealthy,
        }
        return _replace_object(doc, _set_hint(tg, "health_check", mapped))


class LbStickiness(DifferenceRule):
    id = "LB-STICKINESS"
    family = "loadbalancing"
    title = "Target-group stickiness becomes session affinity"
    impact = "ALB cookies become Google-generated or application cookies on the backend service."
    strategies = (
        Strategy(
            id="cookie_affinity",
            title="Cookie-based session affinity",
            effect="clients stick to one backend by cookie, as before",
            risk="low",
        ),
    )
    default = "cookie_affinity"

    def detect(self, ir: IRDocument) -> list[Difference]:
        return [
            self.difference([tg.id], type=tg.stickiness.type)
            for tg in _tgs(ir)
            if tg.stickiness is not None
        ]

    def apply(self, ir, diff, strategy_id, params=None) -> IRDocument:
        doc, tg = _subject(ir, diff)
        if tg is None:
            return doc
        if strategy_id != "cookie_affinity":
            raise ValueError(f"unknown strategy {strategy_id!r}")
        kind = tg.stickiness.type
        affinity: dict[str, Any] = {
            "lb_cookie": {"mode": "GENERATED_COOKIE"},
            "app_cookie": {"mode": "HTTP_COOKIE", "cookie": tg.stickiness.cookie_name},
            "source_ip": {"mode": "CLIENT_IP"},
        }[kind]
        if tg.stickiness.duration:
            affinity["ttl"] = tg.stickiness.duration
        return _replace_object(doc, _set_hint(tg, "affinity", affinity))


def _is_https(protocol: str) -> bool:
    return protocol.upper() in {"HTTPS", "TLS"}


class LbCertificates(DifferenceRule):
    id = "LB-CERTIFICATES"
    family = "loadbalancing"
    title = "ACM certificates cannot leave AWS"
    impact = (
        "ACM does not export private keys. A Google-managed certificate for the same domains is "
        "issued automatically, but only once those domains resolve to the new load balancer, so "
        "HTTPS is unavailable between cutover and issuance. Wildcard domains need a certificate "
        "you bring."
    )
    strategies = (
        Strategy(
            id="google_managed",
            title="Google-managed certificate",
            effect=(
                "a certificate for the same domains is issued after DNS points at the new address"
            ),
            risk="medium",
        ),
        Strategy(
            id="manual",
            title="Bring your own certificate",
            effect="this HTTPS listener is not generated until you supply a certificate",
            risk="high",
            manual=True,
        ),
    )
    default = "google_managed"

    def detect(self, ir: IRDocument) -> list[Difference]:
        diffs = []
        for lb in _lbs(ir):
            for listener in lb.listeners:
                if not _is_https(listener.protocol):
                    continue
                domains = sorted({d for c in listener.certificates for d in c.domains})
                diffs.append(
                    self.difference(
                        [lb.id],
                        port=listener.port,
                        domains=domains,
                        wildcard=any(d.startswith("*") for d in domains) or not domains,
                        # Google-managed certificates attach only to global load balancers.
                        nlb=lb.lb_type == "network" or lb.scheme == "internal",
                    )
                )
        return diffs

    def difference(self, subjects: list[str], **context: Any) -> Difference:
        # One difference per HTTPS listener, so the key carries the port.
        return Difference(
            key=f"{self.id}@{subjects[0]}:{context['port']}",
            rule_id=self.id,
            subjects=subjects,
            context=context,
        )

    def allowed_strategies(self, diff: Difference) -> tuple[Strategy, ...]:
        # TLS listeners on an NLB would need a proxy load balancer; wildcards need your own cert.
        if diff.context.get("wildcard") or diff.context.get("nlb"):
            return (self.strategy("manual"),)
        return self.strategies

    def default_strategy(self, diff: Difference) -> str:
        return self.allowed_strategies(diff)[0].id

    def apply(self, ir, diff, strategy_id, params=None) -> IRDocument:
        doc, lb = _subject(ir, diff)
        if lb is None:
            return doc
        if strategy_id not in {s.id for s in self.allowed_strategies(diff)}:
            raise ValueError(f"strategy {strategy_id!r} is not offered for {lb.id}")
        port = str(diff.context["port"])
        if strategy_id == "manual":
            skipped = sorted({*lb.target_hints.get("skipped_listeners", []), int(port)})
            return _replace_object(doc, _set_hint(lb, "skipped_listeners", skipped))
        certs = {**lb.target_hints.get("certificates", {}), port: diff.context["domains"]}
        return _replace_object(doc, _set_hint(lb, "certificates", certs))


def _ssl_profile(policy: str | None) -> dict[str, str]:
    name = (policy or "").upper()
    if "TLS13-1-3" in name:
        return {"profile": "RESTRICTED", "min_tls": "TLS_1_2"}
    if "TLS13" in name or "TLS-1-2" in name or "FS-1-2" in name:
        return {"profile": "MODERN", "min_tls": "TLS_1_2"}
    return {"profile": "COMPATIBLE", "min_tls": "TLS_1_0"}


class LbSslPolicy(DifferenceRule):
    id = "LB-SSL-POLICY"
    family = "loadbalancing"
    title = "ELB security policies map to Google Cloud SSL policies"
    impact = (
        "AWS security policies name exact cipher lists; Google Cloud SSL policies are profiles "
        "with a minimum TLS version. The nearest profile keeps the same minimum version."
    )
    strategies = (
        Strategy(
            id="map_policy",
            title="Nearest SSL policy",
            effect="a Google Cloud SSL policy with the same minimum TLS version",
            risk="low",
        ),
    )
    default = "map_policy"

    def detect(self, ir: IRDocument) -> list[Difference]:
        return [
            Difference(
                key=f"{self.id}@{lb.id}:{listener.port}",
                rule_id=self.id,
                subjects=[lb.id],
                context={"port": listener.port, "policy": listener.ssl_policy},
            )
            for lb in _lbs(ir)
            if lb.lb_type == "application"
            for listener in lb.listeners
            if _is_https(listener.protocol)
        ]

    def apply(self, ir, diff, strategy_id, params=None) -> IRDocument:
        doc, lb = _subject(ir, diff)
        if lb is None:
            return doc
        if strategy_id != "map_policy":
            raise ValueError(f"unknown strategy {strategy_id!r}")
        policies = {
            **lb.target_hints.get("ssl_policies", {}),
            str(diff.context["port"]): _ssl_profile(diff.context["policy"]),
        }
        return _replace_object(doc, _set_hint(lb, "ssl_policies", policies))


class LbSourceRestriction(DifferenceRule):
    id = "LB-SOURCE-RESTRICTION"
    family = "loadbalancing"
    title = "An ALB's security group becomes a Cloud Armor allow-list"
    impact = (
        "A global external Application Load Balancer has no security group. The ALB admitted only "
        "some source ranges; a Cloud Armor policy allowing exactly those ranges keeps it "
        "restricted. Sources given as security groups or prefix lists cannot be expressed."
    )
    strategies = (
        Strategy(
            id="cloud_armor_allowlist",
            title="Cloud Armor allow-list",
            effect=(
                "only the listed source ranges reach the load balancer; everything else gets 403"
            ),
            risk="low",
        ),
        Strategy(
            id="open",
            title="Leave it open",
            effect="the load balancer accepts traffic from anywhere",
            risk="high",
        ),
    )
    default = "cloud_armor_allowlist"

    def detect(self, ir: IRDocument) -> list[Difference]:
        policies = {p.id: p for p in ir.of_kind(FirewallPolicy)}
        diffs = []
        for lb in _lbs(ir):
            if lb.lb_type != "application" or lb.scheme != "internet-facing":
                continue
            cidrs: set[str] = set()
            unmappable = 0
            for policy_id in lb.firewall_policies:
                for rule in policies[policy_id].rules if policy_id in policies else []:
                    if rule.direction != "ingress":
                        continue
                    if isinstance(rule.peer, CidrPeer):
                        cidrs.add(rule.peer.cidr)
                    else:
                        unmappable += 1
            if not cidrs or cidrs & _WORLD:
                continue  # open to the world already, or admits nothing expressible
            diffs.append(
                self.difference([lb.id], cidrs=sorted(cidrs), unmappable_sources=unmappable)
            )
        return diffs

    def risk_for(self, diff: Difference, strategy_id: str) -> Risk:
        if strategy_id == "cloud_armor_allowlist" and diff.context.get("unmappable_sources"):
            return "medium"
        return self.strategy(strategy_id).risk

    def apply(self, ir, diff, strategy_id, params=None) -> IRDocument:
        doc, lb = _subject(ir, diff)
        if lb is None:
            return doc
        if strategy_id == "open":
            return doc
        if strategy_id != "cloud_armor_allowlist":
            raise ValueError(f"unknown strategy {strategy_id!r}")
        return _replace_object(doc, _set_hint(lb, "allowlist", diff.context["cidrs"]))


def _inner_wildcard(path: str) -> bool:
    return "*" in path.rstrip("*") or "?" in path


class LbRuleConditions(DifferenceRule):
    id = "LB-RULE-CONDITIONS"
    family = "loadbalancing"
    title = "Some listener-rule conditions cannot be expressed"
    impact = (
        "Host and path routing carries to the URL map. Rules that also match headers, query "
        "strings, methods or source addresses, or use wildcards other than a trailing '*', are "
        "not generated, so their traffic falls to the next matching rule or the default."
    )
    strategies = (
        Strategy(
            id="drop_rule",
            title="Drop the rule",
            effect="the rule is not generated and is named in the report",
            risk="medium",
        ),
    )
    default = "drop_rule"

    def detect(self, ir: IRDocument) -> list[Difference]:
        diffs = []
        for lb in _lbs(ir):
            dropped = [
                {"port": listener.port, "priority": rule.priority}
                for listener in lb.listeners
                for rule in listener.rules
                if rule.unsupported_conditions or any(_inner_wildcard(p) for p in rule.paths)
            ]
            if dropped:
                diffs.append(self.difference([lb.id], rules=dropped))
        return diffs

    def apply(self, ir, diff, strategy_id, params=None) -> IRDocument:
        doc, lb = _subject(ir, diff)
        if lb is None:
            return doc
        if strategy_id != "drop_rule":
            raise ValueError(f"unknown strategy {strategy_id!r}")
        rules = [*lb.target_hints.get("dropped_rules", []), *diff.context["rules"]]
        return _replace_object(doc, _set_hint(lb, "dropped_rules", rules))


class LbUnsupportedActions(DifferenceRule):
    id = "LB-UNSUPPORTED-ACTIONS"
    family = "loadbalancing"
    title = "Fixed-response and authenticate actions have no URL-map equivalent"
    impact = (
        "A URL map forwards or redirects; it cannot answer with a fixed body or run OIDC/Cognito "
        "authentication. Rules using those actions are dropped. A default fixed-response is "
        "replaced by the lowest-priority forwarding rule's target, so unmatched requests reach a "
        "backend instead of getting the canned response."
    )
    strategies = (
        Strategy(
            id="drop_rule",
            title="Drop those actions",
            effect="rules with these actions are not generated; the report names each",
            risk="medium",
        ),
    )
    default = "drop_rule"

    def detect(self, ir: IRDocument) -> list[Difference]:
        diffs = []
        for lb in _lbs(ir):
            if lb.lb_type != "application":
                continue
            unsupported = {"fixed-response", "authenticate"}
            rules = [
                {"port": listener.port, "priority": rule.priority}
                for listener in lb.listeners
                for rule in listener.rules
                if rule.action.type in unsupported
            ]
            defaults = []
            for listener in lb.listeners:
                if listener.default_action.type not in unsupported:
                    continue
                forwards = [r for r in listener.rules if r.action.type == "forward"]
                fallback = (
                    max(forwards, key=lambda r: r.priority).action.target_groups[0].target_group
                    if forwards and forwards[-1].action.target_groups
                    else None
                )
                defaults.append({"port": listener.port, "fallback": fallback})
            if rules or defaults:
                diffs.append(self.difference([lb.id], rules=rules, defaults=defaults))
        return diffs

    def risk_for(self, diff: Difference, strategy_id: str) -> Risk:
        return "high" if diff.context.get("defaults") else "medium"

    def apply(self, ir, diff, strategy_id, params=None) -> IRDocument:
        doc, lb = _subject(ir, diff)
        if lb is None:
            return doc
        if strategy_id != "drop_rule":
            raise ValueError(f"unknown strategy {strategy_id!r}")
        rules = [*lb.target_hints.get("dropped_rules", []), *diff.context["rules"]]
        updated = _set_hint(lb, "dropped_rules", rules)
        fallbacks = {str(d["port"]): d["fallback"] for d in diff.context["defaults"]}
        if fallbacks:
            updated = _set_hint(updated, "default_fallbacks", fallbacks)
            # A listener with nothing to fall back to cannot be generated at all.
            skipped = [int(p) for p, f in fallbacks.items() if f is None]
            if skipped:
                updated = _set_hint(
                    updated,
                    "skipped_listeners",
                    sorted({*updated.target_hints.get("skipped_listeners", []), *skipped}),
                )
        return _replace_object(doc, updated)


class LbNlbPortTranslation(_AcknowledgeOnly, DifferenceRule):
    id = "LB-NLB-PORT-TRANSLATION"
    family = "loadbalancing"
    title = "A passthrough Network Load Balancer cannot translate ports"
    impact = (
        "The NLB listened on one port and sent traffic to another. A passthrough load balancer "
        "delivers packets to the port they arrived on, so that listener is not generated."
    )
    strategies = (
        Strategy(
            id="manual",
            title="Handle manually",
            effect=(
                "the listener is not generated; move the service to the listener port or use a "
                "proxy Network Load Balancer"
            ),
            risk="high",
            manual=True,
        ),
    )
    default = "manual"

    def detect(self, ir: IRDocument) -> list[Difference]:
        tgs = {tg.id: tg for tg in ir.of_kind(TargetGroup)}
        diffs = []
        for lb in _lbs(ir):
            if lb.lb_type != "network":
                continue
            mismatched = sorted(
                listener.port
                for listener in lb.listeners
                for target in listener.default_action.target_groups
                if target.target_group in tgs
                and tgs[target.target_group].port not in {None, listener.port}
            )
            if mismatched:
                diffs.append(self.difference([lb.id], ports=mismatched))
        return diffs

    def apply(self, ir, diff, strategy_id, params=None) -> IRDocument:
        doc, lb = _subject(ir, diff)
        if lb is None:
            return doc
        if strategy_id != "manual":
            raise ValueError(f"unknown strategy {strategy_id!r}")
        skipped = sorted({*lb.target_hints.get("skipped_listeners", []), *diff.context["ports"]})
        return _replace_object(doc, _set_hint(lb, "skipped_listeners", skipped))


class LbAccessLogs(DifferenceRule):
    id = "LB-ACCESS-LOGS"
    family = "loadbalancing"
    title = "S3 access logs become Cloud Logging request logs"
    impact = "Requests are logged to Cloud Logging instead of an S3 bucket, in a different format."
    strategies = (
        Strategy(
            id="cloud_logging",
            title="Log to Cloud Logging",
            effect="every backend service logs requests to Cloud Logging",
            risk="low",
        ),
    )
    default = "cloud_logging"

    def detect(self, ir: IRDocument) -> list[Difference]:
        return [self.difference([lb.id]) for lb in _lbs(ir) if lb.access_logs]

    def apply(self, ir, diff, strategy_id, params=None) -> IRDocument:
        doc, lb = _subject(ir, diff)
        if lb is None:
            return doc
        if strategy_id != "cloud_logging":
            raise ValueError(f"unknown strategy {strategy_id!r}")
        return _replace_object(doc, _set_hint(lb, "logging", True))
