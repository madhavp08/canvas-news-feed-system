import {
  Body,
  Container,
  Head,
  Hr,
  Html,
  Preview,
  Text,
} from "@react-email/components";
import { EmailSummaryCard } from "./EmailSummaryCard";
import type { NewsFeedEmailProps } from "./types";

function escapeText(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

export function NewsFeedEmail({
  changeType,
  dateRaw,
  items,
  tldr,
  previewText,
}: NewsFeedEmailProps) {
  const header =
    changeType === "NEW_DATE"
      ? `New announcement posted for ${dateRaw}:`
      : `The announcement for ${dateRaw} was edited. Current content:`;

  return (
    <Html>
      <Head />
      <Preview>{previewText}</Preview>
      <Body
        style={{
          margin: 0,
          backgroundColor: "#ffffff",
          fontFamily: "Arial, Helvetica, sans-serif",
        }}
      >
        <Container
          style={{ maxWidth: "600px", margin: "0 auto", padding: "24px 16px" }}
        >
          <EmailSummaryCard tldr={tldr} />
          <Text
            style={{
              fontSize: "15px",
              lineHeight: "22px",
              margin: "0 0 12px",
              color: "#1a1d24",
            }}
          >
            {header}
          </Text>
          {items.map((item, i) => (
            <Text
              key={i}
              style={{
                fontSize: "14px",
                lineHeight: "21px",
                margin: "0 0 8px",
                color: "#2b303a",
                paddingLeft: "4px",
              }}
            >
              {i + 1}. {escapeText(item)}
            </Text>
          ))}
          <Hr style={{ borderColor: "#e5e7eb", margin: "28px 0 16px" }} />
          <Text style={{ color: "#888888", fontSize: "12px", margin: 0 }}>
            Sent by Madhav&apos;s Canvas News Feed Monitor
          </Text>
        </Container>
      </Body>
    </Html>
  );
}
