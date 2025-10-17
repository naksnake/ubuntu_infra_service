
FROM jenkins/jenkins:lts-jdk17
USER root
RUN apt-get update     && apt-get install -y --no-install-recommends curl tftp     && rm -rf /var/lib/apt/lists/*
USER jenkins
EXPOSE 8080 50000
