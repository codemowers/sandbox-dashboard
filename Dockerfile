FROM harbor.k-space.ee/k-space/microservice-base
ADD templates /templates
ADD codemowers-dashboard.py /codemowers-dashboard.py
ENTRYPOINT /codemowers-dashboard.py
