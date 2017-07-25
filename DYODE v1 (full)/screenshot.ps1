#############################################################################
# Capturing a screenshot
# Sources : https://gist.githubusercontent.com/guitarrapc/9870497/raw/9337d89116b7de293f884158aed2d4becb7c0d55/Get-ScreenShot.ps1
#		    http://www.adminarsenal.com/admin-arsenal-blog/capturing-screenshots-with-powershell-and-net/
#############################################################################

param (
    [Parameter(Mandatory=$true)][string]$folder,
    [Parameter(Mandatory=$true)][int]$delay 
)

Add-Type -AssemblyName System.Windows.Forms
Add-type -AssemblyName System.Drawing

# Gather Screen resolution information
$Screen = [System.Windows.Forms.SystemInformation]::VirtualScreen
$Width = $Screen.Width
$Height = $Screen.Height
$Left = $Screen.Left
$Top = $Screen.Top

while ($true) {
# Create bitmap using the top-left and bottom-right bounds
$bitmap = New-Object System.Drawing.Bitmap $Width, $Height

# Create Graphics object
$graphic = [System.Drawing.Graphics]::FromImage($bitmap)

# Capture screen
$graphic.CopyFromScreen($Left, $Top, 0, 0, $bitmap.Size)

# Save to file
$File = $folder + '/screen.jpg'
$bitmap.Save($File, ([system.drawing.imaging.imageformat]::jpeg))

Write-Output "Screenshot saved to: $File"
Write-Output "Waiting $delay ms"
Start-Sleep -Milliseconds $delay

}

