#!/usr/bin/env python3
import os
import random
import sys
import string
import yaml
from sanic import Sanic, response
from kubernetes_asyncio.client.api_client import ApiClient
from kubernetes_asyncio import client, config
from sanic_wtf import SanicForm
from wtforms import BooleanField

fallback_email = "lauri.vosandi@gmail.com"
characters = "abcdefghijkmnpqrstuvwxyz23456789"
app = Sanic("dashboard")
session = {}
_, path = sys.argv

if not app.config.get("WTF_CSRF_SECRET_KEY", ""):
    raise ValueError("SANIC_WTF_CSRF_SECRET_KEY environment variable not set")


class PlaygroundForm(SanicForm):
    pass


with open(path) as fh:
    for kwargs in yaml.safe_load(fh.read()):
        if kwargs["default"]:
            kwargs["render_kw"] = {"checked": ""}
        setattr(PlaygroundForm, kwargs["name"], BooleanField(**kwargs))


@app.middleware("request")
async def add_session(request):
    request.ctx.session = session


async def create_sandbox(email, values):
    identifier = "".join([random.choice(characters) for _ in range(0, 5)])
    values.append({
        "name": "username",
        "value": identifier
    })
    values.append({
        "name": "email",
        "value": email
    })
    j, _ = email.lower().split("@")
    name = "%s-%s" % ("".join([x for x in j if x in string.ascii_letters]), identifier)
    print("Creating sandbox:", name)
    body = {
        "kind": "Application",
        "apiVersion": "argoproj.io/v1alpha1",
        "metadata": {
            "name": name,
        },
        "spec": {
            "project": "default",
            "source": {
                "repoURL": "git@git.k-space.ee:codemowers/lab-template",
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
        await api_instance.create_namespaced_custom_object("argoproj.io", "v1alpha1", "argocd", "applications", body)
        return name


@app.get("/add")
@app.post("/add")
@app.ext.template("add.html")
async def add_namespace_form(request):
    form = PlaygroundForm(request)

    if form.validate():
        sandbox_name = await create_sandbox(request.headers.get("X-Forwarded-User", fallback_email),
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
async def sandbox_delete(request, sandbox_name):
    async with ApiClient() as api:
        api_instance = client.CustomObjectsApi(api)
        v1 = client.CoreV1Api(api)
        await api_instance.delete_namespaced_custom_object("argoproj.io", "v1alpha1", "argocd", "applications", sandbox_name)
        await v1.delete_namespace(sandbox_name)
        return response.redirect("/")


@app.get("/sandbox/<sandbox_name>")
@app.ext.template("detail.html")
async def sandbox_detail(request, sandbox_name):
    async with ApiClient() as api:
        api_instance = client.CustomObjectsApi(api)
        argo_app = await api_instance.get_namespaced_custom_object("argoproj.io", "v1alpha1", "argocd", "applications", sandbox_name)
        return {"sandbox": wrap_sandbox_parameters(argo_app)}


@app.get("/")
@app.ext.template("main.html")
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
            "sandboxes": sandboxes
        }


@app.listener("before_server_start")
async def setup_db(app, loop):
    if os.getenv("KUBECONFIG"):
        await config.load_kube_config()
    else:
        config.load_incluster_config()


app.run(host="0.0.0.0", port=3001, single_process=True, motd=False)
