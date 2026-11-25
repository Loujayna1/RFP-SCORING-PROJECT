import { useState, useEffect, useCallback } from "react";
import { flushSync } from "react-dom";
import { AuthContext } from "./useAuth";
import { rfpApi, setToken, clearToken, getToken } from "../api/client";

// Tokens live in memory only (not localStorage — XSS safe).
// On hard refresh the user is asked to log in again.
export function AuthProvider({ children }) {
  const [user,         setUser]         = useState(null);
  const [initialising, setInitialising] = useState(true);

  useEffect(() => {
    const token = getToken();
    if (!token) {
      setInitialising(false);
      return;
    }
    rfpApi
      .me()
      .then((r) => setUser(r.data))
      .catch(() => clearToken())
      .finally(() => setInitialising(false));
  }, []);

  const login = useCallback(async (email, password) => {
    const res = await rfpApi.login({
      email: email.trim().toLowerCase(),
      password,
    });
    setToken(res.data.access_token);
    // flushSync ensures user state commits before navigate() fires in LoginPage,
    // preventing ProtectedRoute from seeing user=null on the first post-login render.
    flushSync(() => {
      setUser({
        user_id:      res.data.user_id,
        email:        res.data.email,
        role:         res.data.role,
        tenant_id:    res.data.tenant_id,
        display_name: res.data.display_name,
        expires_in:   res.data.expires_in,
      });
    });
    return res.data;
  }, []);

  const logout = useCallback(async () => {
    try { await rfpApi.logout(); } catch { /* ignore */ }
    clearToken();
    setUser(null);
  }, []);

  return (
    <AuthContext.Provider
      value={{ user, login, logout, initialising }}
    >
      {children}
    </AuthContext.Provider>
  );
}
