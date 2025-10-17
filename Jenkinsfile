
pipeline {
  agent any
  environment {
    WEBFS_HOST_IP = "${WEBFS_HOST_IP ?: '127.0.0.1'}"
    WEBFS_PORT    = "${WEBFS_PORT ?: '8080'}"
    ISO_FILE      = "${ISO_FILE ?: 'example.iso'}"
  }
  stages {
    stage('Smoke: Webfs fixed share') {
      steps {
        sh 'curl -sI http://' + WEBFS_HOST_IP + ':' + WEBFS_PORT + '/files/' + ISO_FILE + ' | head -n 1 || true'
        sh 'ls -l data/webfs_share || true'
      }
    }
  }
}
