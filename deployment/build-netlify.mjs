import { mkdir, copyFile, writeFile } from 'node:fs/promises';

const configured = process.env.COLORART_BACKEND_URL;
if (!configured) throw new Error('Set COLORART_BACKEND_URL to the HTTPS origin of the Python server in Netlify environment settings.');
const backend = new URL(configured);
if (backend.protocol !== 'https:' || backend.username || backend.password || backend.pathname !== '/' || backend.search || backend.hash || backend.port) {
  throw new Error('COLORART_BACKEND_URL must be an HTTPS origin without credentials, path, query, fragment or custom port.');
}
await mkdir('dist/static', { recursive: true });
await copyFile('web/index.html', 'dist/index.html');
for (const file of ['app.js', 'style.css']) await copyFile(`web/${file}`, `dist/static/${file}`);
await writeFile('dist/_redirects', `/api/* ${backend.origin}/api/:splat 200!\n`);
console.log('Built Netlify interface and same-origin API proxy.');
