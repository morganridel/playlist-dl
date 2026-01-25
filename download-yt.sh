#!/bin/bash
set -e

ffmpeg="ffmpeg"
timestamps_format=
parse_timestamp=false

help() {
        echo "usage: $(basename "$0") [-f <name>][-h]"
    echo "-h: print help"
    echo "-f: ffmpeg binary path (default to \"ffmpeg\")"
    echo "-p: parse format for 'timestamps' file. use TIME and TITLE specifiers i.e.: -p \"TIME - TITLE\""
    echo "Example:
    $(basename "$0") -p \"TIME | TITLE\" <youtube_url>
    This will parse your video if you have a \`timestamps\` file in the same folder with the following format:
    \`\`\`
    00:00 | Boyfriend Dungeon - Disarmed
    04:19 | Gris - Debris
    07:12 | Jusant - Amer
    10:13 | ICO - Heal
    13:02 | Delver - Sewer
    16:29 | Samorost 2 - Plain
    19:52 | Fe - Skoqen
    \`\`\`"

}

while getopts f:p:h flag
    do
            case "${flag}" in
                    f) ffmpeg="$OPTARG";;
                    p) parse_timestamp=true && timestamps_format="$OPTARG";;
                    h) help ;;
                    *) echo "Invalid option: -$flag." && help ;;
            esac
    done
shift $((OPTIND - 1)) # adjust argument count to parse positional argument after the named ones

# Parse a string with the TIME/TITLE format and set the extracted values globally
function extract_info {
    str=$1
    format=$2
    format=$(echo "$format" | sed 's/[^-A-Za-z0-9_]/\\&/g') # escape regex special characters
    declare -A placeholder_map=(["TIME"]=1 ["TITLE"]=2)

    # Swap the placeholders with regex groups
    format="${format//TIME/(.*)}"
    format="${format//TITLE/(.*)}"

    # Parse the string using the given format
    if [[ $str =~ $format ]]; then
        # Assign global variables.
        TIME=${BASH_REMATCH[placeholder_map["TIME"]]}
        TITLE=${BASH_REMATCH[placeholder_map["TITLE"]]}
    fi
}


youtube_url=$1
if [ -z "$youtube_url" ]; then
    echo "Please specify a youtube url."
    help
    exit 1
fi

# ------------------------------------------------------------------------------------

yt_dlp_common_args=(
    "--write-info-json"
    "--ffmpeg-location $ffmpeg"
    "-v"
    "-x"
    "-f ba[ext=m4a]"
    "--audio-quality 0"
    "--embed-thumbnail"
)

if [ "$parse_timestamp" = true ]; then
    yt-dlp --print-to-file "%(title)s|%(uploader)s" metadata ${yt_dlp_common_args[@]} --output "output.m4a" "$youtube_url"
    echo "Youtube audio downloaded. Starting reading timestamps..."
    IFS='|' read -r album author < metadata
    legal_folder=$(echo "$album" | sed "s/[\\/:*?\"<>|.]//g")
    mkdir -p "$legal_folder"

    count=0
    while IFS= read -r line; do
        echo "Reading line: $line"
        trimmed_line=$(echo "$line" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')
        extract_info "$trimmed_line" "$timestamps_format"

        if [ -n "$previous_time" ]
        then
            echo "Cutting audio from $previous_time to $TIME for '$previous_name'"
            legal_filename=$(echo "$previous_name" | iconv -f utf-8 -t us-ascii//TRANSLIT | sed "s/[\\/:*?\"<>|]//g")
            ffmpeg_common_args=(
                "-loglevel error"
                "-i output.m4a"
            )
            $ffmpeg ${ffmpeg_common_args[@]} -metadata album="$album" -metadata artist="$author" -codec copy -ss "$previous_time" -metadata title="$previous_name" -metadata track="$count" -to "$TIME" "${legal_folder}/$count - ${legal_filename}.m4a" < /dev/null
        fi
        previous_time=$TIME
        previous_name=$TITLE
        count=$((count+1))
    done < "timestamps"

    #special case for last song, put desired end time or remove "-to end_time"
    echo "Cutting audio from $previous_time for '$previous_name'"
    legal_filename=$(echo "$previous_name" | iconv -f utf-8 -t us-ascii//TRANSLIT | sed "s/[\\/:*?\"<>|]//g")
    ffmpeg_common_args=(
        "-loglevel error"
        "-i output.m4a"
    )
    $ffmpeg ${ffmpeg_common_args[@]} -metadata album="$album" -metadata artist="$author" -codec copy -ss "$previous_time" -metadata title="$previous_name" -metadata track="$count" -codec copy -ss "$previous_time" "$legal_folder/$count - $legal_filename.m4a" < /dev/null

    rm "output.m4a"

else
    yt-dlp --print-to-file "%(title)s|%(uploader)s" metadata ${yt_dlp_common_args[@]} --split-chapters --output "chapter:%(section_number)s - %(section_title)s.%(ext)s" --exec rm "$youtube_url"
    # Read title and author from metadata file
    IFS='|' read -r album author < metadata
    legal_folder=$(echo "$album" | sed "s/[\\/:*?\"<>|]//g")
    mkdir -p "$legal_folder"

    # Iterate over all .m4a files in the current directory
    for file in *.m4a; do
        # Create a temporary filename
        temp_file="temp_${file}"

        # Extract track number and track name from filename
        track_number="${file%% -*}"
        track_title="${file#*- }"
        track_title="${track_title%.m4a}"

        # Use ffmpeg to update metadata
        $ffmpeg -i "$file" -metadata album="$album" -metadata artist="$author" -metadata title="$track_title" -metadata track="$track_number" -codec copy "$temp_file"

        # Replace the original file with the modified one
        mv "$temp_file" "$file"
        mv "$file" "$legal_folder/$file"
    done
fi

rm metadata
