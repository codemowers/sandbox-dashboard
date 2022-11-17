FROM harbor.k-space.ee/k-space/microservice-base
ADD app /app
ENTRYPOINT /app/sandbox-dashboard.py
