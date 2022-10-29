#!/usr/bin/env python3
import os
import random
from sanic import Sanic, response
from kubernetes_asyncio.client.api_client import ApiClient
from kubernetes_asyncio import client, config
from sanic_wtf import SanicForm
from wtforms import SubmitField, BooleanField

fallback_email = "lauri.vosandi@gmail.com"
characters = "abcdefghijkmnpqrstuvwxyz23456789"
app = Sanic("dashboard")
app.config["WTF_CSRF_SECRET_KEY"] = os.urandom(50)
session = {}


class PlaygroundForm(SanicForm):
    dex = BooleanField(description="Instantiate Dex for this namespace")
    prometheus = BooleanField("Instantiate Prometheus for this namespace")
    traefik = BooleanField("Instantiate dedicated Traeifk instance in this namespace")
    subdomain = BooleanField("Create dedicated subdomain under codemowers.cloud")
    argocd = BooleanField("Instantiate ArgoCD instance in this namespace")
    submit = SubmitField("Confirm")


@app.middleware("request")
async def add_session(request):
    request.ctx.session = session


async def create_sandbox(email, values):
    identifier = "".join([random.choice(characters) for _ in range(0, 10)])
    values.append({
        "name": "username",
        "value": identifier
    })
    values.append({
        "name": "email",
        "value": email
    })
    name = "sb-%s" % identifier
    print("Creating sandbox:", name)
    body = {
        "kind": "Application",
        "apiVersion": "argoproj.io/v1alpha1",

        "metadata": {
            "name": name
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


@app.get("/add")
@app.post("/add")
@app.ext.template("add.html")
async def add_namespace_form(request):
    form = PlaygroundForm(request)
    if form.validate():
        await create_sandbox(request.headers.get("X-Forwarded-User", fallback_email),
            values=[{"name": key, "value": str(value)} for (key, value) in form.data.items() if key not in ("submit", "csrf_token")])
        return response.redirect("/")

    return {
        "form": form
    }


@app.get("/")
@app.ext.template("main.html")
async def handler(request):
    async with ApiClient() as api:
        api_instance = client.CustomObjectsApi(api)
        sandboxes = []
        for app in (await api_instance.list_namespaced_custom_object("argoproj.io", "v1alpha1", "argocd", "applications"))["items"]:
            values = dict([(p["name"], p.get("value", "")) for p in app["spec"]["source"]["helm"]["parameters"]])
            subdomain = values.get("subdomain", "false").lower() == "true"
            if values.get("email") == request.headers.get("X-Forwarded-User", fallback_email):
                sandboxes.append({
                    "namespace": app["spec"]["destination"]["namespace"],
                    "hostname_suffix": (".%s.codemowers.cloud" if subdomain else "-%s.codemowers.ee") % values.get("username")
                })

        return {
            "sandboxes": sandboxes
        }


@app.listener("before_server_start")
async def setup_db(app, loop):
    if os.getenv("KUBECONFIG"):
        await config.load_kube_config()
    else:
        config.load_incluster_config()


app.run(host="0.0.0.0", port=3001, single_process=True)
