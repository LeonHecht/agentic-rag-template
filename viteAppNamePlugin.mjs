import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const rootDir = path.dirname(fileURLToPath(import.meta.url));
const domainConfigPath = path.join(rootDir, 'backend', 'config', 'domain_config.yaml');

function readDomainAppName() {
  const config = fs.readFileSync(domainConfigPath, 'utf8');
  const match = config.match(/^app_name:\s*(?:"([^"]*)"|'([^']*)'|([^\r\n#]+))/m);
  const appName = (match?.[1] ?? match?.[2] ?? match?.[3] ?? '').trim();

  if (!appName) {
    throw new Error(`Missing app_name in ${domainConfigPath}`);
  }

  return appName;
}

function parseEnvFile(filePath) {
  if (!fs.existsSync(filePath)) {
    return {};
  }

  return Object.fromEntries(
    fs
      .readFileSync(filePath, 'utf8')
      .split(/\r?\n/)
      .map((line) => line.trim())
      .filter((line) => line && !line.startsWith('#'))
      .map((line) => {
        const separator = line.indexOf('=');
        if (separator === -1) {
          return null;
        }

        const key = line.slice(0, separator).trim();
        let value = line.slice(separator + 1).trim();

        if (
          (value.startsWith('"') && value.endsWith('"')) ||
          (value.startsWith("'") && value.endsWith("'"))
        ) {
          value = value.slice(1, -1);
        }

        return [key, value];
      })
      .filter(Boolean),
  );
}

function readEnvAppName(mode, envDir) {
  const envFiles = [
    '.env',
    '.env.local',
    `.env.${mode}`,
    `.env.${mode}.local`,
  ];
  const env = envFiles.reduce(
    (values, fileName) => ({
      ...values,
      ...parseEnvFile(path.join(envDir, fileName)),
    }),
    {},
  );

  return (process.env.VITE_APP_NAME || env.VITE_APP_NAME || '').trim();
}

function escapeHtml(value) {
  return value
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

export function appNamePlugin({ envDir = process.cwd() } = {}) {
  let appName = readDomainAppName();

  return {
    name: 'domain-app-name',
    config(_config, { mode }) {
      appName = readEnvAppName(mode, envDir) || appName;

      return {
        define: {
          'globalThis.__APP_NAME__': JSON.stringify(appName),
        },
      };
    },
    transformIndexHtml(html) {
      return html.replaceAll('%APP_NAME%', escapeHtml(appName));
    },
  };
}
