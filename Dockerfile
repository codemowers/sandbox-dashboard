FROM harbor.k-space.ee/k-space/microservice-base
ADD config /config
ADD app /app
WORKDIR /app
ENTRYPOINT /app/sandbox-dashboard.py /config/playground.yaml
