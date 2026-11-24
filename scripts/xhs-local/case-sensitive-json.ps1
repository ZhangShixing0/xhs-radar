function ConvertFrom-CaseSensitiveJson {
    param(
        [Parameter(Mandatory)]
        [string]$Json
    )

    if ($PSVersionTable.PSVersion.Major -ge 6) {
        return $Json | ConvertFrom-Json -AsHashtable -Depth 100
    }

    Add-Type -AssemblyName System.Web.Extensions
    $serializer = New-Object `
        System.Web.Script.Serialization.JavaScriptSerializer
    $serializer.MaxJsonLength = [int]::MaxValue
    $serializer.RecursionLimit = 200
    return $serializer.DeserializeObject($Json)
}
