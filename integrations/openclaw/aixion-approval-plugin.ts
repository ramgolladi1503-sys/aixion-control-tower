import { spawn } from "node:child_process";
import { definePluginEntry } from "openclaw/plugin-sdk";

type HookDecision = {
  allow?: boolean;
  reason?: string;
};

const MAX_OUTPUT_BYTES = 256 * 1024;
const DEFAULT_TIMEOUT_MS = 60 * 60 * 1000;

function resolveSessionId(event: Record<string, unknown>, context: Record<string, unknown>): string {
  const value =
    process.env.AIXION_RELAY_SESSION_ID ??
    context.sessionId ??
    context.session_id ??
    event.sessionId ??
    event.session_id;
  if (!value) {
    throw new Error("Aixion relay session id is missing.");
  }
  return String(value);
}

function runApprovalHook(
  sessionId: string,
  payload: Record<string, unknown>,
): Promise<HookDecision> {
  return new Promise((resolve, reject) => {
    const executable = process.env.AIXION_RELAY_EXECUTABLE || "aixion-relay";
    const args = ["hook", "openclaw", "--session-id", sessionId];
    const child = spawn(executable, args, {
      shell: false,
      stdio: ["pipe", "pipe", "pipe"],
      env: process.env,
    });
    let stdout = Buffer.alloc(0);
    let stderr = Buffer.alloc(0);
    let settled = false;
    const timeoutMs = Number(
      process.env.AIXION_OPENCLAW_APPROVAL_TIMEOUT_MS || DEFAULT_TIMEOUT_MS,
    );
    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      child.kill("SIGTERM");
      reject(new Error("Aixion approval timed out."));
    }, Math.max(1000, timeoutMs));

    child.stdout.on("data", (chunk: Buffer) => {
      stdout = Buffer.concat([stdout, chunk]);
      if (stdout.length > MAX_OUTPUT_BYTES) {
        child.kill("SIGTERM");
      }
    });
    child.stderr.on("data", (chunk: Buffer) => {
      stderr = Buffer.concat([stderr, chunk]);
      if (stderr.length > MAX_OUTPUT_BYTES) {
        child.kill("SIGTERM");
      }
    });
    child.on("error", (error) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      reject(error);
    });
    child.on("close", (code) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      const text = stdout.toString("utf8").trim();
      if (!text) {
        reject(
          new Error(
            `Aixion approval returned no decision (exit ${code}): ${stderr
              .toString("utf8")
              .trim()}`,
          ),
        );
        return;
      }
      try {
        const decision = JSON.parse(text) as HookDecision;
        resolve(decision);
      } catch (error) {
        reject(new Error(`Invalid Aixion approval response: ${String(error)}`));
      }
    });

    child.stdin.end(JSON.stringify(payload));
  });
}

export default definePluginEntry({
  id: "aixion-approval",
  name: "Aixion Exact Action Approval",
  description:
    "Routes OpenClaw tool calls through Aixion capability policy and mobile approval.",
  register(api) {
    api.on("before_tool_call", async (event, context) => {
      try {
        const sessionId = resolveSessionId(
          event as Record<string, unknown>,
          context as Record<string, unknown>,
        );
        const decision = await runApprovalHook(sessionId, {
          toolName: event.toolName,
          input: event.params,
          event,
          context,
        });
        if (decision.allow === true) {
          return {};
        }
        return {
          block: true,
          blockReason: decision.reason || "Aixion denied this exact tool call.",
        };
      } catch (error) {
        return {
          block: true,
          blockReason: `Aixion approval failed closed: ${String(error)}`,
        };
      }
    });
  },
});
