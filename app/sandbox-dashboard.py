#!/usr/bin/env python3
import argparse
import os
import random
import yaml
from jinja2 import Template
from functools import wraps
from prometheus_client import Gauge
from sanic import Sanic, response
from kubernetes_asyncio.client.api_client import ApiClient
from kubernetes_asyncio.client.exceptions import ApiException
from kubernetes_asyncio import client, config
from sanic_wtf import SanicForm
from time import time
from wtforms import BooleanField


gauge_user_last_seen_timestamp = Gauge(
    "sandbox_user_last_seen",
    "Timestamp of last user interaction",
    ["username"])


HTTP_REQUEST_HEADER_USERNAME = os.getenv("HTTP_REQUEST_HEADER_USERNAME",
    "X-Auth-Request-Preferred-Username")
HTTP_REQUEST_HEADER_EMAIL = os.getenv("HTTP_REQUEST_HEADER_EMAIL",
    "X-Auth-Request-Email")

parser = argparse.ArgumentParser(description="Run Kubernetes cluster sandbox dashboard")
parser.add_argument("--feature-flags-spec",
    default="/config/playground.yml")
args = parser.parse_args()

ANNOTATION_MANAGED_BY = "sandbox-dashboard"

fallback_email = "lauri.vosandi@gmail.com"
characters = "abcdefghijkmnpqrstuvwxyz23456789"
app = Sanic("dashboard")
session = {}

if not app.config.get("WTF_CSRF_SECRET_KEY", ""):
    raise ValueError("SANIC_WTF_CSRF_SECRET_KEY environment variable not set")


class PlaygroundForm(SanicForm):
    pass


with open(args.feature_flags_spec) as fh:
    sandbox_config = yaml.safe_load(fh.read())
    assert "cluster" in sandbox_config
    assert "name" in sandbox_config["cluster"]
    assert "server" in sandbox_config["cluster"]
    assert "oidc-issuer-url" in sandbox_config["cluster"]
    for feature_flag in sandbox_config["features"]:
        if feature_flag.get("disabled"):
            continue
        kwargs = {
            "name": feature_flag["name"],
            "description": feature_flag["description"],
            "default": feature_flag["default"],
            "render_kw": {"checked": ""} if feature_flag["default"] else {}
        }
        setattr(PlaygroundForm, kwargs["name"], BooleanField(**kwargs))


@app.middleware("request")
async def add_session(request):
    request.ctx.session = session


def login_required(*foo):
    def wrapper(func):
        @wraps(func)
        async def wrapped(request, *args, **kwargs):
            # TODO: Move to OIDC
            preferred_username = request.headers.get(HTTP_REQUEST_HEADER_USERNAME)
            email = request.headers.get(HTTP_REQUEST_HEADER_EMAIL)
            request.ctx.user = None
            async with ApiClient() as api:
                api_instance = client.CustomObjectsApi(api)
                try:
                    request.ctx.user = await api_instance.get_cluster_custom_object(
                        "codemowers.io", "v1alpha1", "clusterusers", preferred_username)
                except ApiException as e:
                    if e.status == 404:
                        pass
                    else:
                        raise

                if not request.ctx.user:
                    body = {
                        "apiVersion": "codemowers.io/v1alpha1",
                        "kind": "ClusterUser",
                        "metadata": {
                            "name": preferred_username
                        }, "spec": {
                            "email": email
                        }
                    }
                    request.ctx.user = await api_instance.create_cluster_custom_object(
                        "codemowers.io", "v1alpha1", "clusterusers", body)
                gauge_user_last_seen_timestamp.labels(preferred_username).set(time())
                return await func(request, *args, **kwargs)
        return wrapped
    return wrapper


async def create_sandbox(user, values):
    labels = {
        "codemowers.io/clusteruser": user["metadata"]["name"],
    }
    identifier = "".join([random.choice(characters) for _ in range(0, 5)])
    values.append({
        "name": "username",
        "value": user["metadata"]["name"]
    })
    values.append({
        "name": "email",
        "value": user["spec"]["email"]
    })
    name = "%s-%s" % (user["metadata"]["name"], identifier)
    print("Creating sandbox:", name)
    body = {
        "kind": "Application",
        "apiVersion": "argoproj.io/v1alpha1",
        "metadata": {
            "name": name,
            "labels": labels,
            "annotations": {
                "app.kubernetes.io/managed-by": ANNOTATION_MANAGED_BY
            }
        },
        "spec": {
            "project": "default",
            "source": {
                "repoURL": sandbox_config["template"],
                "path": "./",
                "targetRevision": "HEAD",
                "helm": {
                    "releaseName": name,
                    "parameters": values
                }
            }, "destination": {
                "server": "https://kubernetes.default.svc",
                "namespace": name,
            }, "syncPolicy": {
                "automated": {
                    "prune": True
                }, "syncOptions": [
                    "CreateNamespace=true"
                ]
            }
        }
    }
    async with ApiClient() as api:
        api_instance = client.CustomObjectsApi(api)
        v1 = client.CoreV1Api(api)
        await api_instance.create_namespaced_custom_object("argoproj.io", "v1alpha1", "argocd", "applications", body)
        await v1.create_namespace({
            "metadata": {
                "name": name,
                "labels": labels
            }
        })
        return name


@app.get("/add")
@app.post("/add")
@app.ext.template("add.html")
@login_required()
async def add_sandbox_form(request):
    form = PlaygroundForm(request)
    if form.validate():
        sandbox_name = await create_sandbox(request.ctx.user,
            values=[{"name": key, "value": str(value)} for (key, value) in form.data.items() if key not in ("submit", "csrf_token")])
        return response.redirect("/sandbox/%s" % sandbox_name)
    return {
        "form": form
    }


def wrap_sandbox_parameters(app):
    app_source = app["spec"].get("source", {})
    app_helm = app_source.get("helm", {})
    params = dict([(p["name"], p.get("value", "")) for p in app_helm.get("parameters", {})])
    subdomain = params.get("subdomain", "false").lower() == "true"
    return {
        "namespace": app["metadata"]["name"],
        "hostname_suffix": (".%s.codemowers.cloud" if subdomain else "-%s.codemowers.ee") % params.get("username"),
        "parameters": params
    }


@app.get("/sandbox/<sandbox_name>/delete")
@login_required()
async def sandbox_delete(request, sandbox_name):
    async with ApiClient() as api:
        api_instance = client.CustomObjectsApi(api)
        v1 = client.CoreV1Api(api)
        await api_instance.delete_namespaced_custom_object("argoproj.io", "v1alpha1", "argocd", "applications", sandbox_name)
        await v1.delete_namespace(sandbox_name)
        return response.redirect("/")


@app.get("/sandbox/<sandbox_name>")
@app.ext.template("detail.html")
@login_required()
async def sandbox_detail(request, sandbox_name):
    async with ApiClient() as api:
        api_instance = client.CustomObjectsApi(api)
        v1 = client.CoreV1Api(api)
        network_api = client.NetworkingV1Api(api)
        argo_app = await api_instance.get_namespaced_custom_object("argoproj.io", "v1alpha1", "argocd", "applications", sandbox_name)
        pods = [j.to_dict() for j in (await v1.list_namespaced_pod(sandbox_name)).items]
        sandbox = wrap_sandbox_parameters(argo_app)
        sandbox["cluster"] = sandbox_config["cluster"]

        # Generate sandbox links
        sandbox["links"] = []
        for link in sandbox_config["sandboxLinks"]:
            j = link.copy()
            feature_flag = j.get("feature")
            if feature_flag:
                if not sandbox["parameters"].get(feature_flag):
                    continue
            j["url"] = Template(j["url"]).render({"sandbox": sandbox})
            sandbox["links"].append(j)

        # Generate pod links
        for pod in pods:
            pod["links"] = []
            for link in sandbox_config["podLinks"]:
                j = link.copy()
                feature_flag = j.get("feature")
                if feature_flag:
                    if not sandbox["parameters"].get(feature_flag):
                        continue
                j["url"] = Template(j["url"]).render({"sandbox": sandbox})
                pod["links"].append(j)

        return {
            "args": args,
            "namespace": sandbox_name,
            "sandbox": sandbox,
            "pods": pods,
            "ingress": (await network_api.list_namespaced_ingress(sandbox_name)).items
        }


@app.get("/")
@app.ext.template("main.html")
@login_required()
async def handler(request):
    email = request.headers.get("X-Forwarded-User")
    async with ApiClient() as api:
        api_instance = client.CustomObjectsApi(api)

        sandboxes = []
        for app in (await api_instance.list_namespaced_custom_object("argoproj.io", "v1alpha1", "argocd", "applications"))["items"]:
            if "deletionTimestamp" in app["metadata"]:
                # Hide sandboxes that are about to be deleted
                continue
            w = wrap_sandbox_parameters(app)
            if not email or w["parameters"].get("email") == email:
                sandboxes.append(w)

        return {
            "args": args,
            "feature_flags": sandbox_config["features"],
            "sandboxes": sandboxes
        }


@app.listener("before_server_start")
async def setup_db(app, loop):
    if os.getenv("KUBECONFIG"):
        await config.load_kube_config()
    else:
        config.load_incluster_config()


app.run(host="0.0.0.0", port=3001, single_process=True, motd=False)
