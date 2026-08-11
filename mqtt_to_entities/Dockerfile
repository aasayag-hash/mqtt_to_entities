# Base images pulled from build.yaml normally provide multi-arch python via s6-overlay.
# Kept simple/functional here with a plain python slim image; a production add-on would
# instead use the ${BUILD_FROM} arg wired up through build.yaml for real multi-arch builds.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt

COPY backend /app/backend
COPY frontend /app/frontend
COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD ["/run.sh"]
