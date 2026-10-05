/** @type {import('next').NextConfig} */
const nextConfig = {
  // The Python engine serves the API on :8080. Same-origin /api/* calls
  // from the browser are proxied there, so the UI never talks cross-origin.
  async rewrites() {
    return [
      { source: "/api/:path*", destination: "http://127.0.0.1:8080/api/:path*" },
    ];
  },
};

export default nextConfig;
