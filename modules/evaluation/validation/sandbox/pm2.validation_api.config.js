/**
 * pm2.validation_api.config.js — PM2 ecosystem config for the sandbox Validation API
 *
 * Usage:
 *   pm2 start modules/evaluation/validation/sandbox/pm2.validation_api.config.js
 *   pm2 logs alphacore-validation-api
 */

const path = require('path');

// `sandbox/` lives under `modules/evaluation/validation/`.
// We want the repo root.
const repoRoot = path.resolve(__dirname, '../../../..');
const startScript = path.resolve(__dirname, './start_validation_api_pm2.sh');
const namespace = process.env.PM2_NAMESPACE || 'alphacore';

module.exports = {
  apps: [
    {
      name: process.env.PROCESS_NAME || 'alphacore-validation-api',
      namespace,
      script: startScript,
      interpreter: '/bin/bash',
      cwd: repoRoot,
      env: {
        VENV_DIR: process.env.VENV_DIR || path.join(repoRoot, '.venv-validation-api'),
        ENV_FILE: process.env.ENV_FILE || path.join(repoRoot, '.env.validation_api'),

        // Required: token mint/refresh (service account JSON key)
        ALPHACORE_GCP_CREDS_FILE: process.env.ALPHACORE_GCP_CREDS_FILE || path.join(repoRoot, 'gcp-creds.json'),

        // Validation API bind
        ALPHACORE_VALIDATION_HTTP_HOST: process.env.ALPHACORE_VALIDATION_HTTP_HOST || '127.0.0.1',
        ALPHACORE_VALIDATION_HTTP_PORT: process.env.ALPHACORE_VALIDATION_HTTP_PORT || '8888',

        // Sandbox runner execution (default matches setup.sh sudoers rule)
        ALPHACORE_SANDBOX_PYTHON: process.env.ALPHACORE_SANDBOX_PYTHON || '/usr/bin/python3',
        ALPHACORE_SANDBOX_USE_SUDO: process.env.ALPHACORE_SANDBOX_USE_SUDO || 'true',
      },
      autorestart: true,
      max_restarts: 10,
      restart_delay: 5000,
      log_date_format: 'YYYY-MM-DD HH:mm:ss Z',
      error_file: path.join(repoRoot, 'logs', 'pm2', 'validation_api.error.log'),
      out_file: path.join(repoRoot, 'logs', 'pm2', 'validation_api.out.log'),
      merge_logs: true,
    },
  ],
};
