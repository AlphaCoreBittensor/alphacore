/**
 * pm2.config.js — PM2 ecosystem config for AlphaCore validator
 * Usage:
 *   pm2 start scripts/validator/process/pm2.config.js --env production
 *   pm2 status
 *   pm2 logs alphacore-validator
 */

const path = require('path');

const repoRoot = path.resolve(__dirname, '../../..');
const startScript = path.resolve(__dirname, './start_validator_pm2.sh');
const namespace = process.env.PM2_NAMESPACE || 'alphacore';

module.exports = {
  apps: [
    {
      name: process.env.PROCESS_NAME || 'alphacore-validator',
      namespace,
      script: startScript,
      interpreter: '/bin/bash',
      cwd: repoRoot,
      env: {
        // Entry point: neurons/validator.py (standard) or scripts/start_validator.py (dual services)
        // Default to dual services; override to 'neurons/validator.py' for single service
        ENTRYPOINT: process.env.ENTRYPOINT || 'scripts/start_validator.py',
        // venv & env file locations
        VENV_DIR: process.env.VENV_DIR || path.join(repoRoot, 'venv'),
        ENV_FILE: process.env.ENV_FILE || '',
        // Bittensor config via .env (sourced in start script)
        // You can override here if you prefer:
        // ALPHACORE_NETUID: '1',
        // ALPHACORE_CHAIN_ENDPOINT: 'ws://127.0.0.1:9945',
        // ALPHACORE_WALLET_NAME: 'validator-a',
        // ALPHACORE_WALLET_HOTKEY: 'validator-a',
      },
      // Restart strategy
      autorestart: true,
      max_restarts: 10,
      restart_delay: 5000,
      // Logs
      log_date_format: 'YYYY-MM-DD HH:mm:ss Z',
      error_file: path.join(
        process.env.LOG_DIR || path.join(repoRoot, 'logs', 'pm2'),
        `${process.env.PROCESS_NAME || 'alphacore-validator'}.error.log`,
      ),
      out_file: path.join(
        process.env.LOG_DIR || path.join(repoRoot, 'logs', 'pm2'),
        `${process.env.PROCESS_NAME || 'alphacore-validator'}.out.log`,
      ),
      merge_logs: true,
    },
  ],
};
