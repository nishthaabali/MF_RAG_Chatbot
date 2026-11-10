/* Where the chat UI finds the API (deployment plan §4.1).
 *
 * "/api" means the same origin: the FastAPI server serves this UI locally. On Vercel the build
 * step (vercel.json) overwrites this file with the Railway URL, e.g.
 * "https://<service>.up.railway.app/api".
 */
window.MF_API_BASE = "/api";
