import { createApp } from "./app.ts";
const port = Number(process.env.PORT ?? 4318);
// Loopback only. A public origin reaches this listener through a reverse proxy.
const host = "127.0.0.1";
const origin = process.env.PUBLIC_ORIGIN ?? `http://127.0.0.1:${port}`;
const service = createApp({
  dbPath: process.env.DB_PATH ?? ".runtime/consult.sqlite",
  origin,
  turnstileSecret: process.env.HERMES_CONSULT_TURNSTILE_SECRET,
  controlToken: process.env.CONSULT_CONTROL_TOKEN ?? "",
  proxyHops: Number(process.env.CONSULT_PROXY_HOPS ?? 0) || 0,
});
const server = service.app.listen(port, host, () =>
  console.log(`Consultation server on ${host}:${port} for ${origin}`),
);
for (const signal of ["SIGINT", "SIGTERM"] as const)
  process.on(signal, () => {
    service.close();
    server.close(() => process.exit(0));
  });

// A crashed parent must not leave a consultation listener orphaned.
if (process.env.CONSULT_PARENT_PID) {
  const parent = Number(process.env.CONSULT_PARENT_PID);
  setInterval(() => {
    try {
      process.kill(parent, 0);
    } catch {
      service.close();
      server.close(() => process.exit(0));
    }
  }, 5000).unref();
}
