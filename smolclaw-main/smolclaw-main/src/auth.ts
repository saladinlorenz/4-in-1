import { randomBytes, createHash } from "node:crypto";
import { execSync } from "node:child_process";
import { platform, userInfo, homedir } from "node:os";
import { readFileSync, writeFileSync, existsSync, mkdirSync, chmodSync } from "node:fs";
import { join } from "node:path";
import { getLogger } from "./logger.ts";

// Same OAuth config as Claude Code
const OAUTH = {
  CLIENT_ID: "9d1c250a-e61b-44d9-88ed-5944d1962f5e",
  AUTHORIZE_URL: "https://claude.ai/oauth/authorize",
  TOKEN_URL: "https://platform.claude.com/v1/oauth/token",
  REDIRECT_PREFIX: "http://localhost",
  SCOPES: ["user:profile", "user:inference", "user:sessions:claude_code", "user:mcp_servers"],
  KEYCHAIN_SERVICE: "Claude Code-credentials",
};

export interface OAuthCredentials {
  claudeAiOauth: {
    accessToken: string;
    refreshToken: string;
    expiresAt: number;
    scopes: string[];
    subscriptionType?: string;
    rateLimitTier?: string;
  };
}

// --- PKCE helpers ---

function generateCodeVerifier(): string {
  return randomBytes(32).toString("base64url");
}

function generateCodeChallenge(verifier: string): string {
  return createHash("sha256").update(verifier).digest("base64url");
}

function generateState(): string {
  return randomBytes(16).toString("hex");
}

// --- Keychain access (macOS) ---

function getKeychainAccount(): string {
  try {
    return process.env.USER || userInfo().username;
  } catch {
    return "claude-code-user";
  }
}

export function readCredentials(): OAuthCredentials | null {
  if (platform() === "darwin") {
    try {
      const account = getKeychainAccount();
      const raw = execSync(
        `security find-generic-password -a "${account}" -s "${OAUTH.KEYCHAIN_SERVICE}" -w`,
        { encoding: "utf-8", stdio: ["pipe", "pipe", "pipe"] }
      ).trim();
      return JSON.parse(raw);
    } catch {
      return null;
    }
  }

  // Linux/other: fall back to plaintext file
  try {
    const credPath = join(homedir(), ".claude", ".credentials.json");
    if (existsSync(credPath)) {
      return JSON.parse(readFileSync(credPath, "utf-8"));
    }
  } catch {
    // ignore
  }
  return null;
}

function writeCredentials(creds: OAuthCredentials): void {
  const json = JSON.stringify(creds);

  if (platform() === "darwin") {
    const account = getKeychainAccount();
    // Delete existing first (ignore errors)
    try {
      execSync(
        `security delete-generic-password -a "${account}" -s "${OAUTH.KEYCHAIN_SERVICE}"`,
        { stdio: "pipe" }
      );
    } catch {
      // ignore
    }
    execSync(
      `security add-generic-password -a "${account}" -s "${OAUTH.KEYCHAIN_SERVICE}" -w "${json.replace(/"/g, '\\"')}"`,
      { stdio: "pipe" }
    );
    return;
  }

  // Linux/other: plaintext file
  const dir = join(homedir(), ".claude");
  if (!existsSync(dir)) mkdirSync(dir, { recursive: true });
  const credPath = join(dir, ".credentials.json");
  writeFileSync(credPath, json, { encoding: "utf-8" });
  chmodSync(credPath, 0o600);
}

// --- Token refresh ---

export async function refreshAccessToken(creds: OAuthCredentials): Promise<OAuthCredentials> {
  const log = getLogger();
  const refreshToken = creds.claudeAiOauth.refreshToken;

  const body = new URLSearchParams({
    grant_type: "refresh_token",
    refresh_token: refreshToken,
    client_id: OAUTH.CLIENT_ID,
    scope: OAUTH.SCOPES.join(" "),
  });

  const res = await fetch(OAUTH.TOKEN_URL, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body,
  });

  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Token refresh failed (${res.status}): ${text}`);
  }

  const data = await res.json() as any;

  const updated: OAuthCredentials = {
    claudeAiOauth: {
      accessToken: data.access_token,
      refreshToken: data.refresh_token ?? refreshToken,
      expiresAt: Date.now() + (data.expires_in ?? 3600) * 1000,
      scopes: creds.claudeAiOauth.scopes,
      subscriptionType: creds.claudeAiOauth.subscriptionType,
      rateLimitTier: creds.claudeAiOauth.rateLimitTier,
    },
  };

  writeCredentials(updated);
  log.info("Access token refreshed");
  return updated;
}

// --- Get valid access token (auto-refresh if expired) ---

export async function getAccessToken(): Promise<string> {
  let creds = readCredentials();
  if (!creds) {
    throw new Error("Not authenticated. Run the app interactively to log in.");
  }

  // Refresh if expired or expiring within 5 minutes
  if (creds.claudeAiOauth.expiresAt < Date.now() + 5 * 60 * 1000) {
    creds = await refreshAccessToken(creds);
  }

  return creds.claudeAiOauth.accessToken;
}

// --- Interactive OAuth login flow ---

export async function login(): Promise<OAuthCredentials> {
  const codeVerifier = generateCodeVerifier();
  const codeChallenge = generateCodeChallenge(codeVerifier);
  const state = generateState();

  // Start local HTTP server to receive callback
  let resolveCode: (code: string) => void;
  const codePromise = new Promise<string>((resolve) => {
    resolveCode = resolve;
  });

  const server = Bun.serve({
    port: 0, // Random available port
    async fetch(req) {
      const url = new URL(req.url);
      if (url.pathname === "/callback") {
        const code = url.searchParams.get("code");
        const returnedState = url.searchParams.get("state");

        if (!code || returnedState !== state) {
          return new Response("Authentication failed: invalid state or missing code.", {
            status: 400,
            headers: { "Content-Type": "text/html" },
          });
        }

        resolveCode!(code);

        return new Response(
          `<html><body style="font-family:system-ui;display:flex;justify-content:center;align-items:center;height:100vh;margin:0;background:#1a1a2e;color:#e0e0e0">
            <div style="text-align:center">
              <h1>Authenticated!</h1>
              <p>You can close this tab and return to the terminal.</p>
            </div>
          </body></html>`,
          { headers: { "Content-Type": "text/html" } }
        );
      }
      return new Response("Not found", { status: 404 });
    },
  });

  const port = server.port;
  const redirectUri = `${OAUTH.REDIRECT_PREFIX}:${port}/callback`;

  // Build authorization URL
  const authUrl = new URL(OAUTH.AUTHORIZE_URL);
  authUrl.searchParams.set("client_id", OAUTH.CLIENT_ID);
  authUrl.searchParams.set("response_type", "code");
  authUrl.searchParams.set("redirect_uri", redirectUri);
  authUrl.searchParams.set("scope", OAUTH.SCOPES.join(" "));
  authUrl.searchParams.set("state", state);
  authUrl.searchParams.set("code_challenge", codeChallenge);
  authUrl.searchParams.set("code_challenge_method", "S256");

  // Open browser
  console.log("\nOpening browser for authentication...\n");
  const openCmd = platform() === "darwin" ? "open" : "xdg-open";
  try {
    execSync(`${openCmd} "${authUrl.toString()}"`, { stdio: "pipe" });
  } catch {
    console.log(`Could not open browser automatically. Please visit:\n${authUrl.toString()}\n`);
  }

  console.log("Waiting for authentication...");

  // Wait for the callback
  const code = await codePromise;
  server.stop();

  // Exchange code for tokens
  const tokenBody = new URLSearchParams({
    grant_type: "authorization_code",
    code,
    redirect_uri: redirectUri,
    client_id: OAUTH.CLIENT_ID,
    code_verifier: codeVerifier,
    state,
  });

  const tokenRes = await fetch(OAUTH.TOKEN_URL, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: tokenBody,
  });

  if (!tokenRes.ok) {
    const text = await tokenRes.text();
    throw new Error(`Token exchange failed (${tokenRes.status}): ${text}`);
  }

  const tokenData = await tokenRes.json() as any;

  const creds: OAuthCredentials = {
    claudeAiOauth: {
      accessToken: tokenData.access_token,
      refreshToken: tokenData.refresh_token,
      expiresAt: Date.now() + (tokenData.expires_in ?? 3600) * 1000,
      scopes: OAUTH.SCOPES,
    },
  };

  writeCredentials(creds);
  console.log("\nAuthenticated successfully!\n");

  return creds;
}

// --- Check if already authenticated ---

export function isAuthenticated(): boolean {
  const creds = readCredentials();
  return creds !== null && !!creds.claudeAiOauth?.accessToken;
}
