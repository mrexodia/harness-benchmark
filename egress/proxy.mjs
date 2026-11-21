// Egress proxy for one run. The agent container sits on an internal Docker
// network whose only way out is this process: an HTTP forward proxy (plain
// HTTP and CONNECT) that allows hosts on the allowlist and logs every request,
// allowed or not, as one JSON line on stdout.
import http from "node:http";
import net from "node:net";
import { readFileSync } from "node:fs";

const PORT = Number(process.env.PORT || 3128);
const patterns = readFileSync(process.env.ALLOWLIST || "/egress/allowlist.txt", "utf8")
  .split("\n").map((l) => l.replace(/#.*/, "").trim().toLowerCase()).filter(Boolean);
const extra = (process.env.EXTRA_ALLOW || "").split(",").map((s) => s.trim().toLowerCase()).filter(Boolean);
const allow = [...patterns, ...extra];
// An entry may name a port ("host:5601"): that port is then allowed too,
// including as a CONNECT tunnel (Node tunnels plain-HTTP requests that way).
// Without one, a host is allowed on 443 (CONNECT) and on any port for plain HTTP.
const rules = allow.map((p) => { const m = p.match(/^(.*):(\d+)$/); return m ? { host: m[1], port: Number(m[2]) } : { host: p, port: null }; });
const hostMatch = (p, host) => (p.startsWith("*.") ? host.endsWith(p.slice(1)) || host === p.slice(2) : host === p);
const allowed = (host, port) => {
  host = host.toLowerCase();
  return rules.some((r) => hostMatch(r.host, host) && (port == null || r.port === port || (r.port == null && port === 443)));
};
const log = (o) => process.stdout.write(JSON.stringify({ t: new Date().toISOString(), ...o }) + "\n");

const server = http.createServer((req, res) => {
  let url;
  try { url = new URL(req.url); } catch { res.writeHead(400).end(); return; }
  const ok = url.protocol === "http:" && allowed(url.hostname, null);
  log({ kind: "http", method: req.method, host: url.hostname, port: url.port || 80, path: url.pathname, allowed: ok });
  if (!ok) { res.writeHead(403, { "content-type": "text/plain" }).end("blocked by benchmark egress policy\n"); return; }
  const headers = { ...req.headers };
  delete headers["proxy-connection"]; delete headers["proxy-authorization"];
  const up = http.request({ host: url.hostname, port: url.port || 80, method: req.method, path: url.pathname + url.search, headers }, (r) => {
    res.writeHead(r.statusCode, r.headers); r.pipe(res);
  });
  up.on("error", (e) => { log({ kind: "error", host: url.hostname, error: e.message }); res.destroy(); });
  req.pipe(up);
});

server.on("connect", (req, sock, head) => {
  const [host, portStr] = req.url.split(":");
  const port = Number(portStr || 443);
  const ok = allowed(host, port);
  log({ kind: "connect", host, port, allowed: ok });
  if (!ok) { sock.end("HTTP/1.1 403 Forbidden\r\n\r\n"); return; }
  const up = net.connect(port, host, () => {
    sock.write("HTTP/1.1 200 Connection Established\r\n\r\n");
    if (head?.length) up.write(head);
    up.pipe(sock); sock.pipe(up);
  });
  const close = () => { up.destroy(); sock.destroy(); };
  up.on("error", (e) => { log({ kind: "error", host, error: e.message }); close(); });
  sock.on("error", close);
});

server.listen(PORT, "0.0.0.0", () => log({ kind: "start", port: PORT, allow }));
