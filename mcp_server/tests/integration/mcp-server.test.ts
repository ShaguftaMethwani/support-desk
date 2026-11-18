import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js";
import { registerTools } from "../../src/server/tool-registry.js";
import { GmailService } from "../../src/services/gmail.service.js";
import { GoogleDocsService } from "../../src/services/google-docs.service.js";

function createMockGmailService(): GmailService {
  return {
    createDraft: vi.fn().mockResolvedValue({
      success: true,
      draft_id: "integration-draft-id",
      message: "Draft created successfully.",
    }),
  } as unknown as GmailService;
}

function createMockDocsService(): GoogleDocsService {
  return {
    appendContent: vi.fn().mockResolvedValue({
      success: true,
      document_id: "integration-doc-id",
      revision_id: "rev-9",
      skipped: false,
      message: "Content appended successfully.",
    }),
  } as unknown as GoogleDocsService;
}

/** A real MCP client talking to the server over an in-memory transport. */
describe("MCP Server — over the MCP protocol", () => {
  let client: Client;
  let server: McpServer;
  let gmailService: GmailService;
  let docsService: GoogleDocsService;

  beforeEach(async () => {
    server = new McpServer({ name: "test-server", version: "1.0.0" });
    gmailService = createMockGmailService();
    docsService = createMockDocsService();
    registerTools(server, gmailService, docsService);
    const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
    client = new Client({ name: "test-client", version: "1.0.0" });
    await Promise.all([server.connect(serverTransport), client.connect(clientTransport)]);
  });

  afterEach(async () => {
    await client.close();
    await server.close();
  });

  it("lists exactly the two allowed tools and no send tool", async () => {
    const { tools } = await client.listTools();
    expect(tools.map((t) => t.name).sort()).toEqual(["gmail_create_draft", "google_docs_append"]);
  });

  it("gmail_create_draft does not require a recipient", async () => {
    const { tools } = await client.listTools();
    const draft = tools.find((t) => t.name === "gmail_create_draft");
    expect(draft?.inputSchema.required ?? []).not.toContain("to");
    expect(draft?.inputSchema.required).toEqual(expect.arrayContaining(["subject", "body"]));
  });

  it("google_docs_append accepts a dedupe_key", async () => {
    const { tools } = await client.listTools();
    const append = tools.find((t) => t.name === "google_docs_append");
    expect(Object.keys(append?.inputSchema.properties ?? {})).toContain("dedupe_key");
  });

  it("calls gmail_create_draft without a recipient and returns the draft id", async () => {
    const result = await client.callTool({
      name: "gmail_create_draft",
      arguments: { subject: "Weekly pulse", body: "Body" },
    });
    const content = result.content as Array<{ text: string }>;
    expect(JSON.parse(content[0].text).draft_id).toBe("integration-draft-id");
    expect(gmailService.createDraft).toHaveBeenCalledWith(expect.objectContaining({ to: [] }));
  });

  it("refuses a call to the removed send tool", async () => {
    const result = await client.callTool({
      name: "gmail_send_email",
      arguments: { to: ["a@example.com"], subject: "s", body: "b" },
    });
    expect(result.isError).toBe(true);
  });
});
