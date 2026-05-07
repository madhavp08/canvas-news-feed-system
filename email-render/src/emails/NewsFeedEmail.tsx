import {
  Body,
  Container,
  Head,
  Hr,
  Html,
  Link,
  Section,
  Text,
} from "@react-email/components";
import { Fragment } from "react";
import { EmailSummaryCard } from "./EmailSummaryCard";
import type { NewsFeedEmailProps, ItemSegment } from "./types";

const PROMO_URL_SPLIT = /(https?:\/\/[^\s]+)/gi;

function promoHrefAllowed(href: string): boolean {
  const lc = href.trim().toLowerCase();
  return lc.startsWith("https://") || lc.startsWith("http://");
}

function PromoLinkified({ text }: { text: string }) {
  const chunks = text.split(PROMO_URL_SPLIT).filter((c) => c.length > 0);
  return (
    <>
      {chunks.map((chunk, i) =>
        /^https?:\/\//i.test(chunk) && promoHrefAllowed(chunk) ? (
          <Link
            key={i}
            href={chunk.trim()}
            style={{
              color: "#0b57d0",
              textDecoration: "underline",
            }}
          >
            {chunk.trim()}
          </Link>
        ) : (
          <Fragment key={i}>{chunk}</Fragment>
        ),
      )}
    </>
  );
}

const promoBoxStyle = {
  border: "1px solid #e2e6ef",
  borderRadius: "8px",
  padding: "14px 16px",
  margin: "20px 0 0",
  backgroundColor: "#f9fafb",
} as const;

function PromoCallout({ promoText }: { promoText: string }) {
  return (
    <Section style={promoBoxStyle}>
      <Text
        style={{
          margin: 0,
          fontSize: "14px",
          lineHeight: "21px",
          color: "#2b303a",
          fontFamily: "Arial, Helvetica, sans-serif",
        }}
      >
        <PromoLinkified text={promoText} />
      </Text>
    </Section>
  );
}

function BulletLine(props: { index: number; segments: ItemSegment[] }) {
  const { index, segments } = props;

  const body = segments.map((s, si) =>
    s.type === "text" ? (
      <Fragment key={`t-${si}`}>{s.text}</Fragment>
    ) : (
      <Link
        key={`l-${si}`}
        href={s.href}
        style={{
          color: "#0b57d0",
          textDecoration: "underline",
        }}
      >
        {s.label}
      </Link>
    ),
  );

  return (
    <Text
      style={{
        fontSize: "14px",
        lineHeight: "21px",
        margin: "0 0 8px",
        color: "#2b303a",
        paddingLeft: "4px",
      }}
    >
      {index + 1}. {body}
    </Text>
  );
}

export function NewsFeedEmail({
  changeType,
  dateRaw,
  items,
  tldr,
  previewText,
  promoText,
}: NewsFeedEmailProps) {
  const header =
    changeType === "NEW_DATE"
      ? `New announcement posted for ${dateRaw}:`
      : `The announcement for ${dateRaw} was edited. Current content:`;

  return (
    <Html>
      <Head />
      <Body
        style={{
          margin: 0,
          backgroundColor: "#ffffff",
          fontFamily: "Arial, Helvetica, sans-serif",
        }}
      >
        {/* Preheader must live inside <body>. <Preview> renders before <body> and
            breaks HTML; Gmail has been observed to show a completely blank message. */}
        {previewText ? (
          <Section
            style={{
              display: "none",
              maxHeight: "0px",
              overflow: "hidden",
              lineHeight: "1px",
              opacity: 0,
            }}
          >
            <Text style={{ margin: 0, fontSize: "1px", color: "transparent" }}>
              {previewText}
            </Text>
          </Section>
        ) : null}
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
          {items.map((segments, i) => (
            <BulletLine key={i} index={i} segments={segments} />
          ))}
          {promoText ? <PromoCallout promoText={promoText} /> : null}
          <Hr style={{ borderColor: "#e5e7eb", margin: "28px 0 16px" }} />
          <Text style={{ color: "#888888", fontSize: "12px", margin: 0 }}>
            Sent by Madhav&apos;s Canvas News Feed Monitor
          </Text>
        </Container>
      </Body>
    </Html>
  );
}
