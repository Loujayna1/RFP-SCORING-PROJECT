import axios from "axios";

// ── Token store (in-memory, XSS-safe) ────────────────────────────────────────
let _token = null;
export function setToken(t)  { _token = t; }
export function clearToken() { _token = null; }
export function getToken()   { return _token; }

// ── Axios instance ────────────────────────────────────────────────────────────
const api = axios.create({ baseURL: "/api/v1", timeout: 60000 });

// Attach Bearer token to every request
api.interceptors.request.use(config => {
  if (_token) config.headers["Authorization"] = `Bearer ${_token}`;
  return config;
}, err => Promise.reject(err));

// Redirect to /login on 401
api.interceptors.response.use(
  res => res,
  err => {
    if (err.response?.status === 401) {
      clearToken();
      if (!window.location.pathname.startsWith("/login") &&
          !window.location.pathname.startsWith("/signup") &&
          !window.location.pathname.startsWith("/invite")) {
        window.location.href = "/login";
      }
    }
    return Promise.reject(err);
  },
);

// ── API surface ───────────────────────────────────────────────────────────────
export const rfpApi = {

  // ── Auth ──────────────────────────────────────────────────────────────────
  login:         (body)        => api.post("/auth/login", body),
  me:            ()            => api.get("/auth/me"),
  logout:        ()            => api.post("/auth/logout"),
  acceptInvite:  (body)        => api.post("/auth/invite/accept", body),

  // ── Tenant ────────────────────────────────────────────────────────────────
  registerTenant: (body)       => api.post("/tenants/register", body),
  getTenant:      ()           => api.get("/tenants/me"),

  // ── User management ───────────────────────────────────────────────────────
  listUsers:     ()            => api.get("/users"),
  inviteUser:    (body)        => api.post("/users/invite", body),
  deactivateUser:(userId)      => api.delete(`/users/${userId}`),

  // ── RFP assignments ───────────────────────────────────────────────────────
  listAssignments:  (rfpId)           => api.get(`/rfp/${rfpId}/assignments`),
  assignEvaluator:  (rfpId, body)     => api.post(`/rfp/${rfpId}/assignments`, body),
  removeAssignment: (rfpId, aid)      => api.delete(`/rfp/${rfpId}/assignments/${aid}`),

  // ── RFP state machine ─────────────────────────────────────────────────────
  transitionRfp: (rfpId, body)        => api.post(`/rfp/${rfpId}/transition`, body),

  // ── RFP packages ──────────────────────────────────────────────────────────
  list:          ()            => api.get("/rfp"),
  create:        (form)        => api.post("/rfp", form),
  get:           (id)          => api.get(`/rfp/${id}`),
  analyze:       (form)        => api.post("/rfp/analyze", form),
  deleteRfp:     (rfpId)       => api.delete(`/rfp/${rfpId}`),

  // ── Vendor responses ──────────────────────────────────────────────────────
  addVendor:     (rfpId, form)          => api.post(`/rfp/${rfpId}/vendor`, form),
  score:         (rfpId, resId)         => api.get(`/rfp/${rfpId}/vendor/${resId}/score`),
  coverage:      (rfpId, resId)         => api.get(`/rfp/${rfpId}/vendor/${resId}/coverage`),
  team:          (rfpId, resId)         => api.get(`/rfp/${rfpId}/vendor/${resId}/team`),
  deleteVendor:  (rfpId, responseId)    => api.delete(`/rfp/${rfpId}/vendor/${responseId}`),
  updateScore:   (rfpId, responseId, body) =>
    api.post(`/rfp/${rfpId}/vendor/${responseId}/score`, body),

  // ── Business rep votes ────────────────────────────────────────────────────
  submitVote:    (rfpId, responseId, body) =>
    api.post(`/rfp/${rfpId}/vendor/${responseId}/vote`, body),
  getVotes:      (rfpId)                => api.get(`/rfp/${rfpId}/votes`),

  // ── Rubric approval ───────────────────────────────────────────────────────
  getRubric:     (rfpId)                => api.get(`/rfp/${rfpId}/rubric`),
  updateRubric:  (rfpId, body)          => api.patch(`/rfp/${rfpId}/rubric`, body),
  lockRubric:    (rfpId, body)          => api.post(`/rfp/${rfpId}/rubric/lock`, body),
  rubricStatus:  (rfpId)                => api.get(`/rfp/${rfpId}/rubric/status`),

  // ── Evaluator dashboard + submit ──────────────────────────────────────────
  getEvaluatorDashboard: (rfpId)        => api.get(`/rfp/${rfpId}/evaluator/dashboard`),
  submitEvaluation:      (rfpId)        => api.post(`/rfp/${rfpId}/evaluator/submit`, { confirm: true }),

  // ── Score overrides (human review) ───────────────────────────────────────
  getVendorReview: (rfpId, resId)       => api.get(`/rfp/${rfpId}/vendor/${resId}/review`),
  overrideScore:   (rfpId, resId, scoreId, body) =>
    api.patch(`/rfp/${rfpId}/vendor/${resId}/review/${scoreId}`, body),

  // ── Consensus ─────────────────────────────────────────────────────────────
  getConsensus:         (rfpId)         => api.get(`/rfp/${rfpId}/consensus`),
  consensusOverride:    (rfpId, body)   => api.patch(`/rfp/${rfpId}/consensus/override`, body),
  saveConsensusNotes:   (rfpId, body)   => api.post(`/rfp/${rfpId}/consensus/notes`, body),
  declareWinner:        (rfpId, body)   => api.post(`/rfp/${rfpId}/declare-winner`, body),
  lockRfp:              (rfpId)         => api.post(`/rfp/${rfpId}/lock`),

  // ── Conflict detection ────────────────────────────────────────────────────
  getConflicts:  (rfpId)                => api.get(`/rfp/${rfpId}/conflicts`),

  // ── Comparison + config ───────────────────────────────────────────────────
  compare:       (rfpId)                => api.get(`/rfp/${rfpId}/compare`),
  getConfig:     (rfpId)                => api.get(`/rfp/${rfpId}/config`),
  updateConfig:  (rfpId, body)          => api.post(`/rfp/${rfpId}/config`, body),

  // ── Requirements (Dataset A = matrix, Dataset B = document / LLM) ─────────
  matrixRequirements: (rfpId) =>
    api.get(`/rfp/${rfpId}/requirements/matrix`),
  documentRequirements: (rfpId, skip = 0, limit = 100) =>
    api.get(`/rfp/${rfpId}/requirements/document`, { params: { skip, limit } }),
  requirements: (rfpId, skip = 0, limit = 100, reqType = null) =>
    api.get(`/rfp/${rfpId}/requirements`, {
      params: {
        skip,
        limit,
        ...(reqType ? { req_type: reqType } : {}),
      },
    }),

  // ── Document content (for in-page PDF / Excel viewer) ────────────────────
  docContent: (docId) => api.get(`/docs/${docId}/content`),

  // ── Reports ───────────────────────────────────────────────────────────────
  genReport:            (rfpId, body)   => api.post(`/rfp/${rfpId}/report`, body),
  genAuditCsv:          (rfpId)         => api.post(`/rfp/${rfpId}/report/audit-csv`),
  genComplianceCert:    (rfpId)         => api.post(`/rfp/${rfpId}/report/compliance-certificate`),
  genVendorDebriefs:    (rfpId)         => api.post(`/rfp/${rfpId}/report/vendor-debriefs`),
  reportStatus:         (reportId)      => api.get(`/reports/${reportId}/status`),
  // responseType blob so the authenticated axios client (which carries the
  // bearer token) streams the file; a plain <a href> navigation has no token
  // and gets 401.
  reportDownload:       (reportId)      => api.get(`/reports/${reportId}/download`, { responseType: "blob" }),

  // ── Tasks + audit ─────────────────────────────────────────────────────────
  taskStatus:    (taskId)               => api.get(`/tasks/${taskId}`),
  auditLog:      (params = {})          => api.get("/audit-log", { params }),

  // ── Chat ──────────────────────────────────────────────────────────────────
  chatSession:   (rfpId, body)          => api.post(`/rfp/${rfpId}/chat/session`, body),
  chatHistory:   (rfpId, sessId)        =>
    api.get(`/rfp/${rfpId}/chat/session/${sessId}/messages`),
  chatArtifacts: (rfpId)                => api.get(`/rfp/${rfpId}/chat/artifacts`),
  createChatArtifact: (rfpId, body)     => api.post(`/rfp/${rfpId}/chat/artifacts`, body),
  deleteChatArtifact: (rfpId, artifactId) =>
    api.delete(`/rfp/${rfpId}/chat/artifacts/${artifactId}`),
};

// ── WebSocket helper (token as ?token= query param) ───────────────────────────
export function openChatSocket(sessionId, { onMessage, onOpen, onClose, onError } = {}) {
  const protocol   = window.location.protocol === "https:" ? "wss:" : "ws:";
  const tokenParam = _token ? `?token=${encodeURIComponent(_token)}` : "";
  const url        = `${protocol}//${window.location.host}/ws/chat/${sessionId}${tokenParam}`;
  const ws         = new WebSocket(url);
  if (onMessage) ws.onmessage = onMessage;
  if (onOpen)    ws.onopen    = onOpen;
  if (onClose)   ws.onclose   = onClose;
  if (onError)   ws.onerror   = onError;
  return ws;
}

export default api;
