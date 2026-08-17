/** @type {import('next').NextConfig} */

// Where to forward /api/* requests on the server side. Set
// NEO_API_TARGET=http://192.168.x.x:8000 if you ever run the FastAPI
// backend on a different host than the Next.js dev server.
const API_TARGET = process.env.NEO_API_TARGET ?? "http://localhost:8000";

const nextConfig = {
  reactStrictMode: true,

  // Emit .next/standalone — a self-contained server.js plus only the
  // node_modules it actually traced. Lets the Docker runtime stage skip
  // `npm ci --omit=dev` entirely (~500 MB -> ~150 MB). No effect on `next dev`.
  output: "standalone",

  // Proxy /api/* on the Next.js server to FastAPI. Two reasons:
  //   1. The browser always hits a same-origin URL → no CORS dance, works
  //      identically on localhost, behind ngrok, on a deployed app, etc.
  //   2. We don't have to bake an absolute backend URL into the client
  //      bundle, which means rotating ngrok URLs doesn't require a rebuild.
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: `${API_TARGET}/:path*`,
      },
    ];
  },
};

export default nextConfig;
