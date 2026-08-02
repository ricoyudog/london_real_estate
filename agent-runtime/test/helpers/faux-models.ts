import {
  createModels,
  fauxProvider,
  type FauxProviderHandle,
  type FauxResponseStep,
} from "@earendil-works/pi-ai";

export const FAUX_PROVIDER = "nan-fung-faux";
export const FAUX_MODEL = "pi";
export const FAUX_MODEL_REF = `${FAUX_PROVIDER}/${FAUX_MODEL}`;

export type FauxModels = {
  readonly models: ReturnType<typeof createModels>;
  readonly faux: FauxProviderHandle;
};

export function createFauxModels(script: readonly FauxResponseStep[]): FauxModels {
  const faux = fauxProvider({
    provider: FAUX_PROVIDER,
    models: [{ id: FAUX_MODEL, reasoning: false }],
  });
  faux.setResponses([...script]);
  const models = createModels();
  models.setProvider(faux.provider);
  Object.assign(models, {
    hasConfiguredAuth: (provider: string) => provider === FAUX_PROVIDER,
    checkAuth: async (provider: string) => provider === FAUX_PROVIDER ? { source: "fixture" } : undefined,
    isUsingOAuth: () => false,
  });
  return { models, faux };
}
