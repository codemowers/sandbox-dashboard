#!/usr/bin/env python3
import argparse
import os
import random
import string
import yaml
from functools import wraps
from sanic import Sanic, response
from kubernetes_asyncio.client.api_client import ApiClient
from kubernetes_asyncio.client.exceptions import ApiException
from kubernetes_asyncio import client, config
from sanic_wtf import SanicForm
from wtforms import BooleanField

parser = argparse.ArgumentParser(description="Run Kubernetes cluster sandbox dashboard")
parser.add_argument("--cluster-name",
    default="codemowers.eu")
parser.add_argument("--cluster-api-url",
    default="https://kube.codemowers.eu")
parser.add_argument("--context-name-prefix",
    default="codemowers.eu/")
parser.add_argument("--sandbox-template-url",
    default="git@git.k-space.ee:codemowers/lab-template")
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
    feature_flags = yaml.safe_load(fh.read())
    for feature_flag in feature_flags:
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
            email = request.headers.get("X-Forwarded-User", fallback_email)
            request.ctx.user = None
            async with ApiClient() as api:
                api_instance = client.CustomObjectsApi(api)
                resp = await api_instance.list_cluster_custom_object("codemowers.io", "v1alpha1", "clusterusers")
                try:
                    # TODO: Figure out better way to do this
                    for user in resp["items"]:
                        if user["spec"]["email"] == email:
                            request.ctx.user = user
                            break
                except ApiException as e:
                    if e.status == 404:
                        pass
                    else:
                        raise

                if not request.ctx.user:
                    j, _ = email.lower().split("@")
                    username = "".join([x for x in j if x in string.ascii_letters])
                    body = {
                        "apiVersion": "codemowers.io/v1alpha1",
                        "kind": "ClusterUser",
                        "metadata": {
                            "name": username
                        }, "spec": {
                            "email": email
                        }
                    }
                    request.ctx.user = await api_instance.create_cluster_custom_object(
                        "codemowers.io", "v1alpha1", "clusterusers", body)
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
        "value": identifier
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
                "repoURL": args.sandbox_template_url,
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
        "namespace": app["spec"]["destination"]["namespace"],
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
        return {
            "args": args,
            "namespace": sandbox_name,
            "sandbox": wrap_sandbox_parameters(argo_app),
            "pods": (await v1.list_namespaced_pod(sandbox_name)).items,
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
            "feature_flags": feature_flags,
            "sandboxes": sandboxes
        }


@app.listener("before_server_start")
async def setup_db(app, loop):
    if os.getenv("KUBECONFIG"):
        await config.load_kube_config()
    else:
        config.load_incluster_config()


app.run(host="0.0.0.0", port=3001, single_process=True, motd=False)
