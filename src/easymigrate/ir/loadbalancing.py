"""Load-balancing intent objects (spec 7 §4). Faithful capture only."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AfterValidator, Field

from easymigrate.ir.base import IntentObject, StrictModel
from easymigrate.ir.network import PolicyRef, SubnetRef

ActionType = Literal["forward", "redirect", "fixed-response", "authenticate"]


def _target_group_ref(value: str) -> str:
    if not value.startswith("target_group:") or value == "target_group:":
        raise ValueError(f"expected a target_group reference, got {value!r}")
    return value


TargetGroupRef = Annotated[str, AfterValidator(_target_group_ref)]


class WeightedTarget(StrictModel):
    target_group: TargetGroupRef
    weight: int = 1


class Redirect(StrictModel):
    protocol: str | None = None
    port: str | None = None
    host: str | None = None
    path: str | None = None
    status: str = "HTTP_301"


class Action(StrictModel):
    type: ActionType
    target_groups: list[WeightedTarget] = Field(default_factory=list)
    redirect: Redirect | None = None


class ListenerRule(StrictModel):
    priority: int
    hosts: list[str] = Field(default_factory=list)
    paths: list[str] = Field(default_factory=list)
    unsupported_conditions: list[str] = Field(default_factory=list)
    action: Action


class Certificate(StrictModel):
    arn: str
    domains: list[str] = Field(default_factory=list)


class Listener(StrictModel):
    port: int
    protocol: str
    certificates: list[Certificate] = Field(default_factory=list)
    ssl_policy: str | None = None
    default_action: Action
    rules: list[ListenerRule] = Field(default_factory=list)


class LoadBalancer(IntentObject):
    kind: Literal["load_balancer"] = "load_balancer"
    lb_type: Literal["application", "network"]
    scheme: Literal["internet-facing", "internal"]
    subnets: list[SubnetRef] = Field(default_factory=list)
    firewall_policies: list[PolicyRef] = Field(default_factory=list)
    listeners: list[Listener] = Field(default_factory=list)
    idle_timeout: int | None = None
    access_logs: bool = False
    deletion_protection: bool = False
    kubernetes_owned: bool = False

    def references(self) -> list[str]:
        refs: list[str] = [*self.subnets, *self.firewall_policies]
        for listener in self.listeners:
            for action in [listener.default_action, *(r.action for r in listener.rules)]:
                refs += [t.target_group for t in action.target_groups]
        return refs


class HealthCheck(StrictModel):
    protocol: str = "HTTP"
    port: str = "traffic-port"
    path: str | None = None
    interval: int = 30
    timeout: int = 5
    healthy: int = 5
    unhealthy: int = 2
    matcher: str | None = None


class Stickiness(StrictModel):
    type: Literal["lb_cookie", "app_cookie", "source_ip"]
    cookie_name: str | None = None
    duration: int | None = None


class TargetGroup(IntentObject):
    kind: Literal["target_group"] = "target_group"
    protocol: str
    port: int | None = None
    target_type: Literal["instance", "ip", "lambda", "alb"] = "instance"
    network: str | None = None
    health_check: HealthCheck = Field(default_factory=HealthCheck)
    stickiness: Stickiness | None = None
    deregistration_delay: int | None = None
    instances: list[str] = Field(default_factory=list)
    groups: list[str] = Field(default_factory=list)
    unresolved_targets: list[str] = Field(default_factory=list)

    def references(self) -> list[str]:
        refs = [*self.instances, *self.groups]
        if self.network:
            refs.append(self.network)
        return refs
