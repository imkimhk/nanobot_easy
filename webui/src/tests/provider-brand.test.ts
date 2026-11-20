import { describe, expect, it } from "vitest";
import { faviconUrls, logoFallbackUrls, providerBrand } from "@/lib/provider-brand";

describe("provider decoration privacy", () => {
  it("does not send visited domains to favicon services", () => {
    expect(faviconUrls("z.ai")).toEqual([]);
    expect(logoFallbackUrls("https://www.google.com/s2/favicons?domain=private.example")).toEqual([]);
  });
  it("allows local assets while rejecting network-path references", () => {
    expect(logoFallbackUrls("/brand/local.svg")).toEqual(["/brand/local.svg"]);
    expect(logoFallbackUrls("//example.com/logo.svg")).toEqual([]);
  });
  it.each(["openai", "zhipu", "longcat", "mimo", "openrouter", "assemblyai", "bocha", "keenable"])(
    "retains %s initials without remote logo requests", (provider) => {
      expect(providerBrand(provider)?.initials).toBeTruthy();
      expect(providerBrand(provider)?.logoUrls).toEqual([]);
    },
  );
});
