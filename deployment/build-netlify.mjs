import { mkdir, copyFile, writeFile, rm } from 'node:fs/promises';

const configured = process.env.COLORART_BACKEND_URL;
const backend = configured ? new URL(configured) : null;
if (backend && (backend.protocol !== 'https:' || backend.username || backend.password || backend.pathname !== '/' || backend.search || backend.hash || backend.port)) {
  throw new Error('COLORART_BACKEND_URL must be an HTTPS origin without credentials, path, query, fragment or custom port.');
}
await mkdir('dist/static', { recursive: true });
await copyFile(backend ? 'web/index.html' : 'web/backend-unavailable.html', 'dist/index.html');
for (const file of ['app.js', 'style.css']) await copyFile(`web/${file}`, `dist/static/${file}`);
if (backend) {
  await writeFile('dist/_redirects', `/api/* ${backend.origin}/api/:splat 200!\n`);
  console.log('Built Netlify interface and same-origin API proxy.');
} else {
  await rm('dist/_redirects', { force: true });
  console.log('Built placeholder page. Configure COLORART_BACKEND_URL to enable the atelier.');
}
