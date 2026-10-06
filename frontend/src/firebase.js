import { initializeApp } from "firebase/app";
import { getAnalytics } from "firebase/analytics";
import { getAuth, GoogleAuthProvider } from "firebase/auth";

export const CANONICAL_FIREBASE_PROJECT_ID = "nckh-c0ef6";

const firebaseConfig = {
  apiKey: "AIzaSyDDPsHgdDyAbyNL4pTQUUInyw9s4VMQVeM",
  authDomain: "nckh-c0ef6.firebaseapp.com",
  projectId: CANONICAL_FIREBASE_PROJECT_ID,
  storageBucket: "nckh-c0ef6.firebasestorage.app",
  messagingSenderId: "953198341983",
  appId: "1:953198341983:web:22a5e6810096bca9be10ea",
  measurementId: "G-BV5PSNEMVP",
};

const app = initializeApp(firebaseConfig);
const hasFirebaseWebConfig = Boolean(firebaseConfig.apiKey && firebaseConfig.appId);
const usesRegisterProject = firebaseConfig.projectId === CANONICAL_FIREBASE_PROJECT_ID;

let authInstance = null;
let analyticsInstance = null;
let googleProviderInstance = null;

if (hasFirebaseWebConfig && usesRegisterProject) {
  try {
    authInstance = getAuth(app);
    analyticsInstance = firebaseConfig.measurementId ? getAnalytics(app) : null;
    googleProviderInstance = new GoogleAuthProvider();
    googleProviderInstance.setCustomParameters({ prompt: "select_account" });
  } catch (error) {
    console.error(`Firebase web app config is invalid for ${CANONICAL_FIREBASE_PROJECT_ID}.`, error);
  }
}

export const isFirebaseConfigured = Boolean(authInstance);
export const analytics = analyticsInstance;
export const auth = authInstance;
export const googleProvider = googleProviderInstance;
