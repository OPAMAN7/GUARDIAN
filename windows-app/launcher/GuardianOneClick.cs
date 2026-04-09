using System;
using System.Diagnostics;
using System.IO;
using System.Windows.Forms;

namespace GuardianOneClick
{
    internal static class Program
    {
        [STAThread]
        private static void Main()
        {
            try
            {
                string baseDir = AppDomain.CurrentDomain.BaseDirectory;
                string scriptPath = Path.Combine(baseDir, "start-live-control.bat");

                if (!File.Exists(scriptPath))
                {
                    MessageBox.Show(
                        "Could not find start-live-control.bat next to this launcher.\n\n" +
                        "Expected path:\n" + scriptPath,
                        "Guardian Shield Launcher",
                        MessageBoxButtons.OK,
                        MessageBoxIcon.Error
                    );
                    return;
                }

                var psi = new ProcessStartInfo
                {
                    FileName = scriptPath,
                    WorkingDirectory = baseDir,
                    UseShellExecute = true,
                };

                Process.Start(psi);
            }
            catch (Exception ex)
            {
                MessageBox.Show(
                    "Failed to launch Guardian Shield.\n\n" + ex.Message,
                    "Guardian Shield Launcher",
                    MessageBoxButtons.OK,
                    MessageBoxIcon.Error
                );
            }
        }
    }
}
