FROM codemowers/python
ADD config /config
ADD app /app
WORKDIR /app
ENTRYPOINT /app/sandbox-dashboard.py /config/playground.yaml
