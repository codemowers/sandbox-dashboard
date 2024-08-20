#!/usr/bin/env python3
import argparse
import os
import prometheus_async
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
    "Remote-Username")

parser = argparse.ArgumentParser(description="Run Kubernetes cluster sandbox dashboard")
parser.add_argument("--config",
    default="/config/playground.yaml")
args = parser.parse_args()

ANNOTATION_MANAGED_BY = "sandbox-dashboard"

characters = "abcdefghijkmnpqrstuvwxyz23456789"
app = Sanic("dashboard")
session = {}

if not app.config.get("WTF_CSRF_SECRET_KEY", ""):
    raise ValueError("SANIC_WTF_CSRF_SECRET_KEY environment variable not set")


class PlaygroundForm(SanicForm):
    pass


with open(args.config) as fh:
    sandbox_config = yaml.safe_load(fh.read())
    assert "cluster" in sandbox_config
    assert "name" in sandbox_config["cluster"]
    assert "server" in sandbox_config["cluster"]
    assert "oidc-issuer-url" in sandbox_config["cluster"]
    assert "registry" in sandbox_config
    assert "hostname" in sandbox_config["registry"]
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
            username = request.headers.get(HTTP_REQUEST_HEADER_USERNAME, "laurivosandi").lower()
            request.ctx.user = None
            async with ApiClient() as api:
                api_instance = client.CustomObjectsApi(api)
                try:
                    request.ctx.user = await api_instance.get_namespaced_custom_object(
                        "codemowers.cloud", "v1beta1", "default", "oidcusers", username)
                except ApiException as e:
                    if e.status == 404:
                        raise ValueError("No such user")
                    else:
                        raise
                return await func(request, *args, **kwargs)
        return wrapped
    return wrapper

@app.route("/add", methods=["GET", "POST"])
@login_required()
async def add_sandbox_form(request):
    username = request.ctx.user["metadata"]["name"]
    identifier = "".join([random.choice(characters) for _ in range(0, 5)])
    name = "sb-%s-%s" % (username, identifier)
    labels = {
        "codemowers.cloud/sandbox-owner": username,
        "env": "sandbox",
    }
    async with ApiClient() as api:
        api_instance = client.CustomObjectsApi(api)
        v1 = client.CoreV1Api(api)

        await v1.create_namespace({
            "metadata": {
                "name": name,
                "labels": labels
            }
        })

        if sandbox_config.get("argo"):
            values = [
                {
                    "name": "username",
                    "value": username
                }
            ]
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
                    "project": sandbox_config["argo"]["project"],
                    "source": {
                        "repoURL": sandbox_config["argo"]["url"],
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
            await api_instance.create_namespaced_custom_object(
                "argoproj.io", "v1alpha1", sandbox_config["argo"]["namespace"],
                "applications", body)
        return response.redirect("/sandbox/%s" % name)



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
        pods = [j.to_dict() for j in (await v1.list_namespaced_pod(sandbox_name)).items]
        sandbox = {}
        ns = await v1.read_namespace(sandbox_name)

        sandbox["owner"] = ns.metadata.labels.get("codemowers.cloud/sandbox-owner", ns.metadata.labels.get("owner", None))
        if not sandbox["owner"]:
            raise
        sandbox["cluster"] = sandbox_config["cluster"]
        sandbox["registry"] = sandbox_config["registry"]

        """
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
        """
        d = {
            "args": args,
            "namespace": sandbox_name,
            "sandbox": sandbox,
            "pods": pods,
            "ingress": (await network_api.list_namespaced_ingress(sandbox_name)).items,
        }

        if ('codemowers.cloud', 'SecretClaim') in app.ctx.crds:
            d["secretclaims"] = (await api_instance.list_namespaced_custom_object(
                "codemowers.cloud", "v1beta1", sandbox_name, "secretclaims"))["items"]

        if ('codemowers.cloud', 'MysqlDatabaseClaim') in app.ctx.crds:
            d["mysqldatabaseclaims"] = (await api_instance.list_namespaced_custom_object(
                "codemowers.cloud", "v1beta1", sandbox_name, "mysqldatabaseclaims"))["items"]
            d["mysqldatabaseclasses"] = (await api_instance.list_cluster_custom_object(
                "codemowers.cloud", "v1beta1", "mysqldatabaseclasses"))["items"]

        if ('codemowers.cloud', 'PostgresDatabaseClaim') in app.ctx.crds:
            d["postgresdatabaseclaims"] = (await api_instance.list_namespaced_custom_object(
                "codemowers.cloud", "v1beta1", sandbox_name, "postgresdatabaseclaims"))["items"]
            d["postgresdatabaseclasses"] = (await api_instance.list_cluster_custom_object(
                "codemowers.cloud", "v1beta1", "postgresdatabaseclasses"))["items"]

        if ('codemowers.cloud', 'KeydbClaim') in app.ctx.crds:
            d["keydbclaims"] = (await api_instance.list_namespaced_custom_object(
                "codemowers.cloud", "v1beta1", sandbox_name, "keydbclaims"))["items"]
            d["keydbclasses"] = (await api_instance.list_cluster_custom_object(
                "codemowers.cloud", "v1beta1", "keydbclasses"))["items"],

        if ('codemowers.cloud', 'RedisClaim') in app.ctx.crds:
            d["redisclaims"] = (await api_instance.list_namespaced_custom_object(
                "codemowers.cloud", "v1beta1", sandbox_name, "redisclaims"))["items"]
            d["redisclasses"] = (await api_instance.list_cluster_custom_object(
                "codemowers.cloud", "v1beta1", "redisclasses"))["items"]

        if ('codemowers.cloud', 'MinioBucketClaim') in app.ctx.crds:
            d["miniobucketclaims"] = (await api_instance.list_namespaced_custom_object(
                "codemowers.cloud", "v1beta1", sandbox_name, "miniobucketclaims"))["items"]
            d["miniobucketclasses"] = (await api_instance.list_cluster_custom_object(
                "codemowers.cloud", "v1beta1", "miniobucketclasses"))["items"]

        if ('dragonflydb.io', 'Dragonfly') in app.ctx.crds:
            d["dragonflies"] = (await api_instance.list_namespaced_custom_object(
                "dragonflydb.io", "v1alpha1", sandbox_name, "dragonflies"))["items"]

        if ('postgresql.cnpg.io', 'Cluster') in app.ctx.crds:
            d["cnpgs"] = (await api_instance.list_namespaced_custom_object(
                "postgresql.cnpg.io", "v1", sandbox_name, "clusters"))["items"]

        if ('mongodbcommunity.mongodb.com', 'MongoDBCommunity') in app.ctx.crds:
            d["mongodbs"] = (await api_instance.list_namespaced_custom_object(
                "mongodbcommunity.mongodb.com", "v1", sandbox_name, "mongodbcommunity"))["items"]

        return d


@app.get("/")
@app.ext.template("main.html")
@login_required()
async def handler(request):
    async with ApiClient() as api:
        v1 = client.CoreV1Api(api)
        sandboxes = []
        for ns in (await v1.list_namespace(label_selector="env==sandbox")).items:
            sandboxes.append(ns)

        return {
            "args": args,
            "sandboxes": sandboxes
        }


@app.listener("before_server_start")
async def setup_db(app, loop):
    if os.getenv("KUBECONFIG"):
        await config.load_kube_config()
    else:
        config.load_incluster_config()

    async with ApiClient() as api:
        api_instance = client.CustomObjectsApi(api)
        v1 = client.CoreV1Api(api)
        app.ctx.crds = dict([((j["spec"]["group"], j["spec"]["names"]["kind"]),
            [i["name"] for i in j["spec"]["versions"]]) for j in
            (await api_instance.list_cluster_custom_object(
            "apiextensions.k8s.io", "v1", "customresourcedefinitions"))["items"]])
        print("Discovered CRD-s:")
        for k,v in sorted(app.ctx.crds.items()):
            print("* %s: %s" % (repr(k), ", ".join(v)))

    app.add_task(prometheus_async.aio.web.start_http_server(port=5000))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=3001, single_process=True, motd=False)
