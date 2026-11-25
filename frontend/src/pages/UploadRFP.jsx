import { useNavigate } from "react-router-dom";
import RFPWizard from "../components/RFPWizard";

export default function UploadRFP() {
  const navigate = useNavigate();

  const handleComplete = (task) => {
    if (task?.status === "error") {
      navigate("/");
      return;
    }
    const rfpId = task?.result?.rfp_id || task?.rfp_id;
    if (rfpId) {
      navigate(`/evaluation/${rfpId}`);
      return;
    }
    navigate("/");
  };

  const handleCancel = () => {
    navigate(-1);
  };

  return (
    <div className="min-h-screen bg-ivory/50">
      <RFPWizard onComplete={handleComplete} onCancel={handleCancel} />
    </div>
  );
}
