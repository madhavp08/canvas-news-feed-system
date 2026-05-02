import { Section, Text } from "@react-email/components";
import type { TldrNormalized } from "./types";

const cardStyle = {
  backgroundColor: "#f4f6fb",
  border: "1px solid #e2e6ef",
  borderRadius: "8px",
  padding: "16px 18px",
  margin: "0 0 24px",
} as const;

const eyebrowStyle = {
  fontSize: "11px",
  letterSpacing: "0.08em",
  textTransform: "uppercase" as const,
  color: "#5c6578",
  margin: "0 0 8px",
  fontFamily: "Arial, Helvetica, sans-serif",
} as const;

const leadStyle = {
  fontSize: "15px",
  lineHeight: "22px",
  color: "#1a1d24",
  margin: "0 0 10px",
  fontWeight: 600,
  fontFamily: "Arial, Helvetica, sans-serif",
} as const;

const lineStyle = {
  fontSize: "14px",
  lineHeight: "21px",
  color: "#2b303a",
  margin: "0 0 6px",
  fontFamily: "Arial, Helvetica, sans-serif",
} as const;

export function EmailSummaryCard({ tldr }: { tldr: TldrNormalized | null }) {
  if (!tldr) {
    return null;
  }

  return (
    <Section style={cardStyle}>
      <Text style={eyebrowStyle}>TL;DR</Text>
      {tldr.kind === "paragraph" ? (
        <Text style={{ ...lineStyle, margin: "0" }}>{tldr.text}</Text>
      ) : (
        <>
          {tldr.lead ? <Text style={leadStyle}>{tldr.lead}</Text> : null}
          {tldr.bullets.map((b, i) => (
            <Text key={i} style={lineStyle}>
              • {b}
            </Text>
          ))}
        </>
      )}
    </Section>
  );
}
