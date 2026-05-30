const DEFAULT_WORKFLOW_FILE = "news-bot.yml";
const DEFAULT_REF = "main";

export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(
      triggerGitHubWorkflow(env, "cloudflare-cron").then((result) => {
        console.log(JSON.stringify(result));
      })
    );
  },

  async fetch(request, env) {
    const url = new URL(request.url);

    if (url.pathname === "/health") {
      return Response.json({
        ok: true,
        service: "dubai-news-bot-cron",
        workflow: env.GITHUB_WORKFLOW_FILE || DEFAULT_WORKFLOW_FILE,
        ref: env.GITHUB_REF || DEFAULT_REF,
      });
    }

    if (url.pathname !== "/run") {
      return new Response("Not found", { status: 404 });
    }

    if (request.method !== "POST") {
      return new Response("Use POST", { status: 405 });
    }

    if (env.RUN_SECRET && request.headers.get("x-run-secret") !== env.RUN_SECRET) {
      return new Response("Unauthorized", { status: 401 });
    }

    const result = await triggerGitHubWorkflow(env, "manual-worker-run");
    return Response.json(result, { status: result.ok ? 200 : 502 });
  },
};

async function triggerGitHubWorkflow(env, reason) {
  const owner = required(env, "GITHUB_OWNER");
  const repo = required(env, "GITHUB_REPO");
  const token = required(env, "GITHUB_TOKEN");
  const workflowFile = env.GITHUB_WORKFLOW_FILE || DEFAULT_WORKFLOW_FILE;
  const ref = env.GITHUB_REF || DEFAULT_REF;

  const response = await fetch(
    `https://api.github.com/repos/${owner}/${repo}/actions/workflows/${workflowFile}/dispatches`,
    {
      method: "POST",
      headers: {
        Accept: "application/vnd.github+json",
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/json",
        "User-Agent": "dubai-news-bot-cloudflare-cron",
        "X-GitHub-Api-Version": "2022-11-28",
      },
      body: JSON.stringify({ ref }),
    }
  );

  if (response.status === 204) {
    return {
      ok: true,
      status: response.status,
      reason,
      repo: `${owner}/${repo}`,
      workflow: workflowFile,
      ref,
      dispatchedAt: new Date().toISOString(),
    };
  }

  const body = await response.text();
  return {
    ok: false,
    status: response.status,
    reason,
    repo: `${owner}/${repo}`,
    workflow: workflowFile,
    ref,
    body: body.slice(0, 1200),
  };
}

function required(env, name) {
  const value = env[name];
  if (!value) {
    throw new Error(`Missing required environment variable: ${name}`);
  }
  return value;
}
