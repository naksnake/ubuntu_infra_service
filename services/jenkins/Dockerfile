FROM jenkins/jenkins:lts-jdk17

USER root

# Install development tools
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        curl \
        tftp-hpa \
        iputils-ping \
        net-tools \
        vim \
        ansible \
        sshpass \
        openssh-client \
        git \
        sudo && \
    rm -rf /var/lib/apt/lists/*

# Create .vim directory and clone indentLine plugin
RUN mkdir -p /root/.vim/pack/vendor/start && \
    git clone https://github.com/Yggdroot/indentLine.git /root/.vim/pack/vendor/start/indentLine && \
    vim -u NONE -c "helptags /root/.vim/pack/vendor/start/indentLine/doc" -c "q"

# Add .vimrc configuration
COPY .vimrc /root/.vim/.vimrc

# Install Jenkins plugins
RUN jenkins-plugin-cli --plugins \
    git \
    workflow-aggregator \
    ansible \
    matrix-auth \
    credentials-binding \
    ssh-slaves \
    blueocean \
    groovy \
    pipeline \
    purge-build-queue-plugin

# Run container as root for full access
USER root

EXPOSE 8080 50000
