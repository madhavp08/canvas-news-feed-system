import { render } from "@react-email/render";
import { readFileSync } from "node:fs";
import { NewsFeedEmail } from "./emails/NewsFeedEmail";
import type { NewsFeedEmailProps } from "./emails/types";

async function main() {
  const raw = readFileSync(0, "utf8").trim();
  if (!raw) {
    console.error("render-cli: empty stdin");
    process.exit(1);
  }
  let props: NewsFeedEmailProps;
  try {
    props = JSON.parse(raw) as NewsFeedEmailProps;
  } catch (e) {
    console.error("render-cli: invalid JSON", e);
    process.exit(1);
  }

  const html = await render(<NewsFeedEmail {...props} />);
  if (!html || !html.trim()) {
    console.error("render-cli: empty HTML output");
    process.exit(1);
  }
  process.stdout.write(html);
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
