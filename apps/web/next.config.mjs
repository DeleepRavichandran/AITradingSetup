/** The browser only ever talks to this origin; /api/* is proxied to the Python API. */
const api = process.env.NSEQL_API_URL || "http://127.0.0.1:8000";
export default {
  output: "standalone",
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${api}/api/:path*` }];
  },
};
