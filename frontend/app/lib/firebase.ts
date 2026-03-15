import { initializeApp, getApps } from "firebase/app";
import { getFirestore } from "firebase/firestore";
import { getStorage } from "firebase/storage";

const firebaseConfig = {
  apiKey: "AIzaSyD6kRYRIeoPeQNO2udqe_Gmb5w5aUgIPFE",
  authDomain: "chronos-cinema.firebaseapp.com",
  projectId: "chronos-cinema",
  storageBucket: "chronos-cinema.firebasestorage.app",
  messagingSenderId: "921111098600",
  appId: "1:921111098600:web:2eac086c6ae5885145855d",
};

const app = getApps().length ? getApps()[0] : initializeApp(firebaseConfig);

export const db = getFirestore(app);
export const storage = getStorage(app);
